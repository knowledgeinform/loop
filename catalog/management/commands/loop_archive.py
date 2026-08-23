"""Operate the JSON archive: export, verify, rebuild, replay, status.

Shaped after ``mongo_admin``: one positional ``action``, destructive ones gated
behind ``--yes``.

The rebuild actions require an explicit ``--target-db``. That is deliberate
friction. ``rebuild`` drops collections, CI runs against a shared Mongo
instance, and a command that defaults to whatever ``MONGODB_URI`` happens to
name is one mistyped shell prompt away from wiping production.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from catalog.archive import rebuild as rebuild_mod
from catalog.archive import registry, writer
from catalog.archive.context import archive_context

ACTIONS = (
    "status",
    "export",
    "verify",
    "rebuild",
    "replay",
)


class Command(BaseCommand):
    help = (
        "Manage the on-disk JSON archive that backs the LOOP catalog. "
        "Actions: status (summary), export (Mongo -> archive), "
        "verify (compare), rebuild (archive -> Mongo), replay (journal -> Mongo)."
    )

    def add_arguments(self, parser):
        parser.add_argument("action", choices=ACTIONS, help="Operation to run")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required for rebuild and replay, which drop collections.",
        )
        parser.add_argument(
            "--kind",
            action="append",
            default=None,
            help=(
                "Limit to one archive kind (repeatable). "
                f"Known kinds: {', '.join(sorted(registry.KINDS))}"
            ),
        )
        parser.add_argument(
            "--target-db",
            default="",
            help="Name of the database rebuild/replay may write to. Must match the "
                 "connected database; required as an explicit confirmation.",
        )
        parser.add_argument(
            "--target-raw-db",
            default="",
            help="Name of the loop_raw database rebuild/replay may write to. Required "
                 "whenever the run touches the raw-file manifest, because that lives "
                 "on a second connection which --target-db does not cover.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="On export, delete archive files that have no matching document.",
        )
        parser.add_argument(
            "--no-derived",
            action="store_true",
            help="On rebuild, skip regenerating embeddings/DOI maps/latest_trial_date.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="On rebuild, read and validate every archived record without "
                 "writing anything to MongoDB or the filesystem. Proves the archive "
                 "is restorable without touching any database.",
        )
        parser.add_argument(
            "--no-raw-files",
            action="store_true",
            help="On rebuild, skip restoring raw uploads from blobs/ into MEDIA_ROOT. "
                 "Only useful when rebuilding into a scratch database that shares "
                 "MEDIA_ROOT with a live one.",
        )
        parser.add_argument(
            "--since",
            default="",
            help="On replay, ignore journal events older than this ISO-8601 timestamp.",
        )
        parser.add_argument(
            "--warn-on-drift",
            action="store_true",
            help="On status, print a warning (but exit 0) when drift is detected.",
        )
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    def handle(self, *args, **options):
        action = options["action"]
        if writer.archive_root() is None:
            raise CommandError(
                "ARCHIVE_ROOT is not configured (or ARCHIVE_ENABLED is off). "
                "Set ARCHIVE_ROOT in the environment before using this command."
            )

        kinds = options["kind"]
        if kinds:
            unknown = [k for k in kinds if k not in registry.KINDS]
            if unknown:
                raise CommandError(f"Unknown archive kind(s): {', '.join(unknown)}")

        with archive_context("system", f"command:loop_archive:{action}"):
            handler = getattr(self, f"_{action}")
            return handler(kinds=kinds, **options)

    # -- status -----------------------------------------------------------

    def _status(self, *, kinds, **options):
        report = rebuild_mod.status()
        if options.get("json"):
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return
        self.stdout.write(f"Archive root: {report.get('root')}")
        schema = report.get("schema") or {}
        self.stdout.write(
            f"Schema version: {schema.get('archive_schema_version', 'not initialized')}"
        )
        self.stdout.write(f"Records: {report.get('total_records', 0)}")
        for name, count in sorted((report.get("records") or {}).items()):
            if count:
                self.stdout.write(f"  {name:24s} {count}")
        self.stdout.write(f"Blob files: {report.get('blob_files', 0)}")
        self.stdout.write(f"Journal days: {report.get('journal_days', 0)}")
        last = report.get("last_event")
        if last:
            self.stdout.write(
                f"Last event: {last.get('ts')} {last.get('op')} "
                f"{last.get('kind')} {last.get('key')} (by {last.get('actor')})"
            )
        drift = report.get("drift_events", 0)
        if drift:
            message = f"{drift} archive write failure(s) recorded in .state/drift.json"
            if options.get("warn_on_drift"):
                self.stdout.write(self.style.WARNING(f"DRIFT: {message}"))
            else:
                raise CommandError(message)
        elif report.get("total_records", 0) == 0:
            self.stdout.write(
                self.style.WARNING(
                    "Archive is empty. If this database already holds data, run: "
                    "manage.py loop_archive export"
                )
            )

    # -- export -----------------------------------------------------------

    def _export(self, *, kinds, **options):
        def progress(message):
            self.stdout.write(message)
            self.stdout.flush()

        results = rebuild_mod.export(
            kinds, prune=options.get("prune", False), progress=progress
        )
        if results.get("django_auth") and results["django_auth"].failed:
            # The accounts live in SQLite, a different database from everything
            # else here. If it is unreachable or unmigrated, say so and keep the
            # catalog export — losing the account roster is bad, abandoning the
            # whole archive is worse.
            self.stdout.write(self.style.ERROR(
                "Django account export failed (see log). The catalog was still "
                "archived; re-run once the Django database is reachable."
            ))
        if options.get("json"):
            self.stdout.write(json.dumps(
                {k: vars(v) for k, v in results.items()}, indent=2
            ))
            return
        total_written = total_unchanged = total_failed = total_deleted = total_missing = 0
        for name, counts in results.items():
            if counts.written or counts.deleted or counts.failed or counts.missing:
                line = (
                    f"  {name:24s} written={counts.written} unchanged={counts.unchanged} "
                    f"deleted={counts.deleted} failed={counts.failed}"
                )
                if counts.missing:
                    line += f" missing-source={counts.missing}"
                self.stdout.write(line)
            total_written += counts.written
            total_unchanged += counts.unchanged
            total_failed += counts.failed
            total_deleted += counts.deleted
            total_missing += counts.missing
        style = self.style.ERROR if total_failed else self.style.SUCCESS
        self.stdout.write(style(
            f"Export complete: {total_written} written, {total_unchanged} unchanged, "
            f"{total_deleted} deleted, {total_failed} failed."
        ))
        total_collisions = sum(c.collisions for c in results.values())
        if total_collisions:
            self.stdout.write(self.style.ERROR(
                f"{total_collisions} archive path collision(s): two or more records "
                "map to the same file, so one has overwritten the other and will be "
                "lost on rebuild. Usually a missing or duplicated key. See the log "
                "for the specific paths."
            ))
        if total_missing:
            self.stdout.write(self.style.WARNING(
                f"{total_missing} trial(s) reference a raw file that is no longer on "
                "disk. Those patterns were lost before the archive existed; the "
                "archive cannot recover them. See the log for the specific trials."
            ))
        if total_failed:
            raise CommandError(f"{total_failed} record(s) failed to export.")

    # -- verify -----------------------------------------------------------

    def _verify(self, *, kinds, **options):
        report = rebuild_mod.verify(kinds)
        if options.get("json"):
            self.stdout.write(json.dumps(vars(report), indent=2, default=str))
            if not report.ok:
                raise CommandError(f"{report.total()} divergence(s) found.")
            return

        for label, entries in (
            ("missing on disk (in Mongo, not archived)", report.missing_on_disk),
            ("missing in Mongo (archived, not loaded)", report.missing_in_db),
            ("content differs", report.content_differs),
        ):
            if entries:
                self.stdout.write(self.style.ERROR(f"{len(entries)} {label}:"))
                for entry in entries[:20]:
                    self.stdout.write(f"    {entry}")
                if len(entries) > 20:
                    self.stdout.write(f"    ... and {len(entries) - 20} more")
        if report.drift_events:
            self.stdout.write(self.style.ERROR(
                f"{len(report.drift_events)} recorded archive write failure(s); "
                "see .state/drift.json"
            ))
        if report.ok:
            self.stdout.write(self.style.SUCCESS("Archive and MongoDB agree."))
            return
        raise CommandError(f"{report.total()} divergence(s) found.")

    # -- rebuild / replay -------------------------------------------------

    def _require_target(self, options, action, kinds=None):
        """Confirm every database this run may overwrite, not just the first.

        LOOP uses two MongoDB connections: the default one and the ``raw``
        alias holding the raw-file manifest. Validating only the default was a
        real hazard — overriding ``MONGODB_URI`` to point at a scratch database
        left ``MONGODB_RAW_URI`` aimed at production, so a "rehearsal" would
        have dropped and repopulated the live ``loop_raw``. Each alias the run
        touches must now be named explicitly.
        """
        from mongoengine.connection import get_db

        if not options.get("yes"):
            raise CommandError(
                f"{action} drops and repopulates collections. Re-run with --yes."
            )

        selected = [k for k in (tuple(kinds) if kinds else rebuild_mod.REBUILD_ORDER)
                    if k in rebuild_mod.REBUILD_ORDER]
        aliases = {registry.kind(k).db_alias for k in selected}

        confirmations = {"default": (options.get("target_db") or "").strip()}
        if "raw" in aliases:
            confirmations["raw"] = (options.get("target_raw_db") or "").strip()

        targets = {}
        for alias, claimed in confirmations.items():
            actual = get_db(alias=alias).name if alias != "default" else get_db().name
            flag = "--target-db" if alias == "default" else "--target-raw-db"
            if not claimed:
                raise CommandError(
                    f"{action} will modify the {alias!r} connection, currently "
                    f"pointing at {actual!r}. Re-run with {flag} {actual} to confirm, "
                    "or restrict the run with --kind. This is an explicit "
                    "confirmation, not a connection setting."
                )
            if claimed != actual:
                raise CommandError(
                    f"{flag} {claimed!r} does not match the connected {alias!r} "
                    f"database {actual!r}. Point the relevant URI at the intended "
                    "database first."
                )
            targets[alias] = actual

        self.stdout.write(self.style.WARNING(
            "Will drop and repopulate: "
            + ", ".join(f"{name} ({alias})" for alias, name in sorted(targets.items()))
        ))
        return targets["default"]

    def _rebuild(self, *, kinds, **options):
        def progress(message):
            self.stdout.write(message)
            self.stdout.flush()

        if options.get("dry_run"):
            # A dry run writes nothing, so there is nothing to confirm.
            self.stdout.write("DRY RUN — validating the archive, writing nothing.")
            target = None
        else:
            target = self._require_target(options, "rebuild", kinds)
        if target:
            self.stdout.write(f"Rebuilding {target} from {writer.archive_root()} ...")
        results = rebuild_mod.rebuild(
            kinds,
            regenerate_derived=not options.get("no_derived", False),
            restore_raw_files=not options.get("no_raw_files", False),
            dry_run=options.get("dry_run", False),
            progress=progress,
        )
        self._report_load(results, options)

    def _replay(self, *, kinds, **options):
        target = self._require_target(options, "replay", kinds)
        since = (options.get("since") or "").strip() or None
        self.stdout.write(f"Replaying journal into {target} ...")
        results = rebuild_mod.replay(since=since)
        self._report_load(results, options)

    def _report_load(self, results, options):
        if options.get("json"):
            self.stdout.write(json.dumps(results, indent=2, default=str))
            return
        for name, count in results.items():
            self.stdout.write(f"  {name:24s} {count}")
        self.stdout.write(self.style.SUCCESS("Done."))
        self.stdout.write(
            "Vector embeddings are not rebuilt here; run "
            "`manage.py backfill_embeddings` if semantic search is in use."
        )

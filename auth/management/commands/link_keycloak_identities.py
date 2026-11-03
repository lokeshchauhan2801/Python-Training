"""link_keycloak_identities — link EXISTING users to their Keycloak accounts.

Keycloak logins are matched only by ``sub`` (auth.OIDCIdentity), never by
email, so an existing user must be linked before they can sign in through
Keycloak. Input is a CSV with the columns ``username,sub`` (sub = the Keycloak
user ID, shown on the user's page in the Keycloak admin console).

The whole file is checked first; if ANY row is invalid (unknown user, user or
sub already linked, or a user/sub repeated in the file) nothing is written.
``--dry-run`` prints the planned links and writes nothing.

    python manage.py link_keycloak_identities admins.csv --dry-run
    python manage.py link_keycloak_identities admins.csv
"""

from __future__ import annotations

import csv

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from auth.models import OIDCIdentity, User


class Command(BaseCommand):
    help = "Link existing users to Keycloak subs from a CSV (columns: username, sub)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--dry-run", action="store_true", help="Print changes, write nothing.")

    def handle(self, *args, csv_path, dry_run, **options):
        with open(csv_path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or {"username", "sub"} - {f.strip() for f in reader.fieldnames}:
                raise CommandError("CSV must have the header: username,sub")
            rows = [
                ((row.get("username") or "").strip(), (row.get("sub") or "").strip(), line)
                for line, row in enumerate(reader, start=2)
            ]

        errors, plan = [], []
        seen_users, seen_subs = set(), set()
        for username, sub, line in rows:
            if not username or not sub:
                errors.append(f"line {line}: username and sub are both required")
                continue
            if username in seen_users:
                errors.append(f"line {line}: user {username!r} appears more than once")
            if sub in seen_subs:
                errors.append(f"line {line}: sub {sub!r} appears more than once")
            seen_users.add(username)
            seen_subs.add(sub)
            user = User.all_objects.filter(username=username).first()
            if user is None:
                errors.append(f"line {line}: no user with username {username!r}")
                continue
            existing = OIDCIdentity.objects.filter(user=user).first()
            if existing:
                errors.append(f"line {line}: user {username!r} is already linked to sub {existing.sub!r}")
            taken = OIDCIdentity.objects.filter(sub=sub).select_related("user").first()
            if taken:
                errors.append(f"line {line}: sub {sub!r} is already linked to user {taken.user.username!r}")
            plan.append((user, sub))

        for user, sub in plan:
            self.stdout.write(f"link {user.username} ({user.email}, staff={user.is_staff}, "
                              f"superuser={user.is_superuser}) -> sub {sub}")
        if errors:
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
            raise CommandError(f"{len(errors)} problem(s) found; nothing was changed.")
        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run: {len(plan)} link(s) would be created; nothing was changed."))
            return
        with transaction.atomic():
            for user, sub in plan:
                OIDCIdentity.objects.create(user=user, sub=sub)
        self.stdout.write(self.style.SUCCESS(f"Created {len(plan)} link(s)."))

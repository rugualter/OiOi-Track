from django.apps import apps
from django.template.defaultfilters import pluralize
from django.utils.translation import gettext_lazy as _
from app import config, helpers
from app.models import MediaTypes, Status
from app.templatetags import app_tags


def process_history_entries(history_records, media_type, media_entry_number, user):
    """Process all history records into timeline entries."""
    timeline_entries = []
    last = history_records.first()

    for _ in range(history_records.count()):
        entry = process_history_entry((last, last.prev_record), media_type, user)
        if entry["changes"]:
            entry["media_entry_number"] = media_entry_number
            timeline_entries.append(entry)
        last = last.prev_record

    return timeline_entries


def process_history_entry(entry, media_type, user):
    """Process a single history entry to organize and format changes."""
    new_record, old_record = entry
    processed_entry = {
        "id": new_record.history_id,
        "date": new_record.history_date,
        "changes": [],
    }

    if old_record is not None:
        return process_changed_entry(
            new_record,
            old_record,
            media_type,
            processed_entry,
            user,
        )
    return process_creation_entry(new_record, media_type, processed_entry, user)


def process_changed_entry(new_record, old_record, media_type, processed_entry, user):
    """Process an entry representing a change to existing media."""
    delta = new_record.diff_against(old_record)
    changes = organize_changes(delta.changes, media_type, user)
    apply_date_status_integration(changes, user)
    build_changes_list(changes, processed_entry)
    return processed_entry


def process_creation_entry(new_record, media_type, processed_entry, user):
    """Process an entry representing media creation."""
    history_model = apps.get_model(
        app_label="app",
        model_name=f"historical{media_type}",
    )

    changes = collect_creation_changes(new_record, history_model, media_type, user)
    apply_date_status_integration(changes, user)
    build_changes_list(changes, processed_entry)
    return processed_entry


def organize_changes(changes, media_type, user):
    """Organize changes into categories."""
    organized = {
        "date_changes": {"start_date": None, "end_date": None},
        "status_change": None,
        "other_changes": [],
    }

    end_date_change = None

    for change in changes:
        if change.field == "progress" and media_type == MediaTypes.MOVIE.value:
            continue

        change_data = {
            "description": format_description(
                change.field,
                change.old,
                change.new,
                media_type,
                user,
            ),
            "field": change.field,
            "old": change.old,
            "new": change.new,
        }

        if change.field == "status":
            organized["status_change"] = change_data
        elif change.field == "end_date":
            end_date_change = change_data
        elif change.field in organized["date_changes"]:
            organized["date_changes"][change.field] = change_data
        else:
            organized["other_changes"].append(change_data)

    if end_date_change:
        organized["date_changes"]["end_date"] = end_date_change

    return organized


def collect_creation_changes(new_record, history_model, media_type, user):
    """Collect changes for a creation entry."""
    organized = {
        "date_changes": {"start_date": None, "end_date": None},
        "status_change": None,
        "other_changes": [],
    }

    for field in history_model._meta.get_fields():
        if (
            field.name.startswith("history_")
            or field.name == "id"
            or not hasattr(new_record, field.attname)
            or (field.name == "progress" and media_type == MediaTypes.MOVIE.value)
        ):
            continue

        value = getattr(new_record, field.attname, None)
        if not value and not (
            media_type == MediaTypes.EPISODE.value and field.name == "end_date"
        ):
            continue

        change_data = {
            "field": field.name,
            "new": value,
            "description": format_description(
                field.name,
                None,
                value,
                media_type,
                user,
            ),
        }

        if field.name == "status":
            organized["status_change"] = change_data
        elif field.name in organized["date_changes"]:
            organized["date_changes"][field.name] = change_data
        elif field.name not in ["item", "user", "related_tv"]:
            organized["other_changes"].append(change_data)

    return organized


def apply_date_status_integration(changes, user):
    """Integrate status changes with date changes where appropriate."""
    date_changes = changes["date_changes"]
    status_change = changes["status_change"]

    # Process start date with status
    if (
        date_changes["start_date"]
        and status_change
        and status_change["new"] == Status.IN_PROGRESS.value
    ):
        date_changes["start_date"]["description"] = _(
            "Started on %(date)s"
        ) % {
            "date": app_tags.datetime_format(
                date_changes["start_date"]["new"],
                user,
            )
        }
        changes["status_change"] = None

    # Process end date with status
    if (
        date_changes["end_date"]
        and status_change
        and status_change["new"] == Status.COMPLETED.value
    ):
        date_changes["end_date"]["description"] = _(
            "Finished on %(date)s"
        ) % {
            "date": app_tags.datetime_format(
                date_changes["end_date"]["new"],
                user,
            )
        }
        changes["status_change"] = None


def build_changes_list(changes, processed_entry):
    """Build the final changes list in the desired order."""
    # Add date changes
    if changes["date_changes"]["start_date"]:
        processed_entry["changes"].append(changes["date_changes"]["start_date"])
    if changes["date_changes"]["end_date"]:
        processed_entry["changes"].append(changes["date_changes"]["end_date"])

    # Add status if not integrated with dates
    if changes["status_change"]:
        processed_entry["changes"].append(changes["status_change"])

    # Add other changes
    processed_entry["changes"].extend(changes["other_changes"])


def format_description(field_name, old_value, new_value, media_type=None, user=None):  # noqa: C901, PLR0911, PLR0912
    """Format change description in a human-readable way.

    Provides natural language descriptions for various types of changes,
    taking into account the media type and status transitions.
    """
    if field_name in {"start_date", "end_date"}:
        new_value = app_tags.datetime_format(new_value, user)
        old_value = app_tags.datetime_format(old_value, user)

    # If old_value is None, treat it as an initial setting
    if old_value is None:
        if field_name == "status":
            action = _("Marked as")
            if new_value == Status.IN_PROGRESS.value:
                verb = config.get_verb(media_type, past_tense=False, gerund=True)
                return _("%(action)s currently %(verb)s") % {
                    "action": action,
                    "verb": verb,
                }

            if new_value == Status.COMPLETED.value:
                return _("%(action)s finished") % {
                    "action": action,
                    "verb": verb,
                }

            if new_value == Status.PLANNING.value:
                verb = config.get_verb(media_type, past_tense=False, gerund=True)
                return _("Added to %(verb)s list") % {
                    "verb": verb,
                }

            if new_value == Status.DROPPED.value:
                return _("%(action)s dropped") % {
                    "action": action,
                }

            if new_value == Status.PAUSED.value:
                verb = config.get_verb(media_type, past_tense=False, gerund=True)
                return _("%(action)s paused") % {
                    "action": action,
                    "verb": verb,
                }

        if field_name == "score":
            return _("Rated %(score)s") % {
                "score": new_value,
            }

        if field_name == "progress" and media_type:
            verb = _(config.get_verb(media_type, past_tense=True)).title()

            if media_type == MediaTypes.GAME.value:
                return _("%(verb)s for %(duration)s") % {
                    "verb": verb,
                    "duration": helpers.minutes_to_hhmm(new_value),
                }

            unit = _(config.get_unit(media_type, short=False).lower())

            return _("%(verb)s up to %(unit)s %(value)s") % {
                "verb": verb,
                "unit": unit,
                "value": new_value,
            }

        if field_name in ["start_date", "end_date"]:
            field_display = _("Started") if field_name == "start_date" else _("Finished")

            if new_value:
                return _("%(field)s on %(date)s") % {
                    "field": field_display,
                    "date": new_value,
                }

            return _("%(field)s without date") % {
                "field": field_display,
            }

        if field_name == "notes":
            return _("Added notes")

        return _("Set %(field)s to %(value)s") % {
            "field": field_name.replace("_", " ").lower(),
            "value": new_value,
        }

    # Regular change (old_value to new_value)
    if field_name == "status":
        verb = _(config.get_verb(media_type, past_tense=False, gerund=True))

        transitions = {
            (
                Status.PLANNING.value,
                Status.IN_PROGRESS.value,
            ): _("Currently %(verb)s") % {"verb": verb},

            (
                Status.IN_PROGRESS.value,
                Status.COMPLETED.value,
            ): _("Finished %(verb)s") % {"verb": verb},

            (
                Status.IN_PROGRESS.value,
                Status.PAUSED.value,
            ): _("Paused %(verb)s") % {"verb": verb},

            (
                Status.PAUSED.value,
                Status.IN_PROGRESS.value,
            ): _("Resumed %(verb)s") % {"verb": verb},

            (
                Status.IN_PROGRESS.value,
                Status.DROPPED.value,
            ): _("Stopped %(verb)s") % {"verb": verb},
        }

        return transitions.get(
            (old_value, new_value),
            _("Changed status from %(old)s to %(new)s") % {
                "old": old_value,
                "new": new_value,
            },
        )

    if field_name == "score":
        if old_value == 0:
            return _("Rated %(score)s") % {
                "score": new_value,
            }

        return _("Changed rating from %(old)s to %(new)s") % {
            "old": old_value,
            "new": new_value,
        }

    if field_name == "progress":
        diff = new_value - old_value
        diff_abs = abs(diff)

        if media_type == MediaTypes.GAME.value:
            if diff > 0:
                return _("Added %(time)s of playtime") % {
                    "time": helpers.minutes_to_hhmm(diff_abs),
                }

            return _("Removed %(time)s of playtime") % {
                "time": helpers.minutes_to_hhmm(diff_abs),
            }

        unit = _(
            config.get_unit(media_type, short=False).lower()
        ) + pluralize(new_value)

        return _("Progress set to %(value)s %(unit)s") % {
            "value": new_value,
            "unit": unit,
        }

    if field_name in ["start_date", "end_date"]:
        field_display = _("Start") if field_name == "start_date" else _("End")
        if not new_value:
            return _("Removed %(field)s date") % {
                "field": field_display.lower(),
            }
        if not old_value:
             return _("%(field)s date set on %(date)s") % {
                "field": field_display,
                "date": new_value,
            }
        return _("%(field)s date changed to %(date)s") % {
                "field": field_display,
                "date": new_value,
            }

    if field_name == "notes":
        if not old_value:
            return _("Added notes")
        if not new_value:
            return _("Removed notes")
        return _("Updated notes")

    field_label = field_name.replace("_", " ").lower()
    return _("Updated %(field)s from %(old)s to %(new)s") % {
        "field": field_label,
        "old": old_value,
        "new": new_value,
    }

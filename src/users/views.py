import logging

import apprise
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_celery_beat.models import PeriodicTask
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from app.models import (
    Item, 
    MediaTypes, 
    Sources, 
    AirOrder, 
    MediaSourceChoices,
    MovieSourceChoices,
    ThemeChoices,
    TVSourceChoices,
    AnimeSourceChoices,
    MangaSourceChoices,
    GameSourceChoices,
    BookSourceChoices,
    ComicSourceChoices,
    BoardGameSourceChoices,
    WatchProviderServicesChoices,
)
from app import helpers
from app.providers import services
from users.forms import NotificationSettingsForm, PasswordChangeForm, UserUpdateForm
from users.models import (
    WATCH_PROVIDER_REGION_UNSET,
    DateFormatChoices,
    QuickWatchDateChoices,
    TimeFormatChoices,
    WeekStartDayChoices,
)

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def account(request):
    """Update the user's account and import/export data."""
    user_form = UserUpdateForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == "POST":
        # Handle username update
        if "username" in request.POST:
            user_form = UserUpdateForm(request.POST, instance=request.user)

            if user_form.is_valid():
                user_form.save()
                messages.success(request, _("Your profile has been updated!"))
                logger.info(
                    "Successful profile change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed profile change for user: %s - %s",
                request.user.username,
                list(user_form.errors.keys()),
            )

        # Handle password update
        elif any(
            key in request.POST
            for key in ["old_password", "new_password1", "new_password2"]
        ):
            password_form = PasswordChangeForm(user=request.user, data=request.POST)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(
                    request,
                    user,
                )
                messages.success(request, _("Your password has been updated!"))
                logger.info(
                    "Successful password change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed password change for user: %s - %s",
                request.user.username,
                list(password_form.errors.keys()),
            )
            
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )

    context = {
        "user_form": user_form,
        "password_form": password_form,
        "order_types": order_types,
        "selected_order_type": selected_order_type,
        "source_choices": MediaSourceChoices.all(),
        
    }

    return render(request, "users/account.html", context)


@require_http_methods(["GET", "POST"])
def notifications(request):
    """Render the notifications settings page."""
    if request.method == "POST":
        form = NotificationSettingsForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Notification settings updated successfully!"))
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, f"{error}")

        return redirect("notifications")

    form = NotificationSettingsForm(instance=request.user)

    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    
    return render(
        request,
        "users/notifications.html",
        {
            "form": form,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        },
    )


@require_GET
def search_items(request):
    """Search for items to exclude from notifications."""
    query = request.GET.get("q", "").strip()

    if not query or len(query) <= 1:
        return render(
            request,
            "users/components/search_results.html",
        )

    # Search for items that match the query
    items = (
        Item.objects.filter(
            Q(title__icontains=query),
        )
        .exclude(
            id__in=request.user.notification_excluded_items.values_list(
                "id",
                flat=True,
            ),
        )
        .distinct()[:10]
    )

    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    
    return render(
        request,
        "users/components/search_results.html",
        {
            "items": items, 
            "query": query,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        },
    )


@require_POST
def exclude_item(request):
    """Exclude an item from notifications."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.add(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()

    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    
    return render(
        request,
        "users/components/excluded_items.html",
        {
            "excluded_items": excluded_items,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        },
    )


@require_POST
def include_item(request):
    """Remove an item from the exclusion list."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.remove(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    return render(
        request,
        "users/components/excluded_items.html",
        {
            "excluded_items": excluded_items,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        },
    )


@require_GET
def test_notification(request):
    """Send a test notification to the user."""
    try:
        # Create Apprise instance
        apobj = apprise.Apprise()

        # Add all notification URLs
        notification_urls = [
            url.strip()
            for url in request.user.notification_urls.splitlines()
            if url.strip()
        ]
        if not notification_urls:
            messages.error(request, _("No notification URLs configured."))
            return redirect("notifications")

        for url in notification_urls:
            apobj.add(url)

        # Send test notification
        result = apobj.notify(
            title=_("OiOi-Track Test Notification"),
            body=(
                _("This is a test notification from OiOi-Track. "),
                _("If you're seeing this, your notifications are working correctly!")
            ),
        )

        if result:
            messages.success(request, _("Test notification sent successfully!"))
        else:
            messages.error(request, _("Failed to send test notification."))
    except Exception:
        logger.exception(_("Error sending notification"))

    return redirect("notifications")


@require_http_methods(["GET", "POST"])
def preferences(request):
    """Render the preferences settings page."""
    media_types = MediaTypes.values
    media_types.remove(MediaTypes.EPISODE.value)
            
    if request.method == "GET":
        
        watch_provider_regions_tmdb = services.get_media_metadata(media_type = "watch_provider_regions", source = Sources.TMDB.value, provider = request.user.watch_provider_tmdb)
        watch_provider_regions_tvdb = services.get_media_metadata(media_type = "watch_provider_regions",source = Sources.TVDB.value, provider = request.user.watch_provider_tmdb)

        order_types = [list(choice) for choice in AirOrder.choices]
    
        selected_order_type = (
            request.user.last_order_type
            or request.user.prefered_air_order
        )
        
        return render(
            request,
            "users/preferences.html",
            {
                "media_types": media_types,
                "quick_watch_date_choices": QuickWatchDateChoices.choices,
                "date_format_choices": DateFormatChoices.choices,
                "time_format_choices": TimeFormatChoices.choices,
                "week_start_day_choices": WeekStartDayChoices.choices,
                "theme_choices": ThemeChoices.choices,
                "movie_source_choices": MovieSourceChoices.choices,
                "tv_source_choices": TVSourceChoices.choices,
                "anime_source_choices": AnimeSourceChoices.choices,
                "manga_source_choices": MangaSourceChoices.choices,
                "game_source_choices": GameSourceChoices.choices,
                "book_source_choices": BookSourceChoices.choices,
                "comic_source_choices": ComicSourceChoices.choices,
                "boardgame_source_choices": BoardGameSourceChoices.choices,
                "tvdb_air_order_choices": AirOrder.choices,
                "watch_provider_choices_tmdb": watch_provider_regions_tmdb,
                "watch_provider_choices_tvdb": watch_provider_regions_tvdb,
                "watch_provider_tmdb": WatchProviderServicesChoices.choices,
                "watch_provider_tvdb": WatchProviderServicesChoices.choices,
                "LANGUAGES": settings.LANGUAGES,
                "order_types": order_types,
                "selected_order_type": selected_order_type,
                "source_choices": MediaSourceChoices.all(),
            },
        )

    
    watch_provider_tmdb = request.POST.get("provider_tmdb")
    if watch_provider_tmdb in WatchProviderServicesChoices.values:
        request.user.watch_provider_tmdb = watch_provider_tmdb
        
    watch_provider_tvdb = request.POST.get("provider_tvdb")
    if watch_provider_tvdb in WatchProviderServicesChoices.values:
        request.user.watch_provider_tvdb = watch_provider_tvdb
    
    watch_provider_regions_tmdb = services.get_media_metadata(media_type = "watch_provider_regions", source = Sources.TMDB.value, provider = watch_provider_tmdb)
    watch_provider_regions_tvdb = services.get_media_metadata(media_type = "watch_provider_regions", source = Sources.TVDB.value, provider = watch_provider_tvdb)
        
    # Prevent demo users from updating preferences
    if request.user.is_demo:
        messages.error(request, _("This section is view-only for demo accounts."))
        return redirect("preferences")

    # Process form submission
    request.user.clickable_media_cards = "clickable_media_cards" in request.POST
    request.user.obfuscate_unseen_episodes = "obfuscate_unseen_episodes" in request.POST
    request.user.quick_watch_date = request.POST.get(
        "quick_watch_date",
        QuickWatchDateChoices.CURRENT_DATE,
    )
    request.user.progress_bar = "progress_bar" in request.POST
    request.user.hide_completed_recommendations = (
        "hide_completed_recommendations" in request.POST
    )
    request.user.hide_zero_rating = "hide_zero_rating" in request.POST
    request.user.date_format = request.POST.get(
        "date_format",
        DateFormatChoices.ISO,
    )
    request.user.time_format = request.POST.get(
        "time_format",
        TimeFormatChoices.HOUR_24,
    )
    week_start_day = request.POST.get("week_start_day")
    if week_start_day in WeekStartDayChoices.values:
        request.user.week_start_day = week_start_day
    media_types_checked = request.POST.getlist("media_types_checkboxes")

    provider_region_tmdb = request.POST.get("watch_provider_region_tmdb", "")
    if provider_region_tmdb in [region[0] for region in watch_provider_regions_tmdb]:
        request.user.watch_provider_region_tmdb = provider_region_tmdb
    else:
        request.user.watch_provider_region_tmdb = WATCH_PROVIDER_REGION_UNSET
        
    provider_region_tvdb = request.POST.get("watch_provider_region_tvdb", "")
    if provider_region_tvdb in [region[0] for region in watch_provider_regions_tvdb]:
        request.user.watch_provider_region_tvdb = provider_region_tvdb
    else:
        request.user.watch_provider_region_tvdb = WATCH_PROVIDER_REGION_UNSET

    # Update user preferences for each media type
    for media_type in media_types:
        setattr(
            request.user,
            f"{media_type}_enabled",
            media_type in media_types_checked,
        )
        
    theme = request.POST.get("theme")
    if theme in [theme_choice[0] for theme_choice in ThemeChoices.choices]:
        request.user.theme = theme
        
    default_movie_source = request.POST.get("movie_source")
    if default_movie_source in MovieSourceChoices.values:
        request.user.default_movie_source = default_movie_source

    default_tv_source = request.POST.get("tv_source")
    if default_tv_source in TVSourceChoices.values:
        request.user.default_tv_source = default_tv_source

    default_anime_source = request.POST.get("anime_source")
    if default_anime_source in AnimeSourceChoices.values:
        request.user.default_anime_source = default_anime_source

    default_manga_source = request.POST.get("manga_source")
    if default_manga_source in MangaSourceChoices.values:
        request.user.default_manga_source = default_manga_source

    default_game_source = request.POST.get("game_source")
    if default_game_source in GameSourceChoices.values:
        request.user.default_game_source = default_game_source

    default_book_source = request.POST.get("book_source")
    if default_book_source in BookSourceChoices.values:
        request.user.default_book_source = default_book_source

    default_comic_source = request.POST.get("comic_source")
    if default_comic_source in ComicSourceChoices.values:
        request.user.default_comic_source = default_comic_source

    default_boardgame_source = request.POST.get("boardgame_source")
    if default_boardgame_source in BoardGameSourceChoices.values:
        request.user.default_boardgame_source = default_boardgame_source
        
    air_order = request.POST.get("air_order")
    if air_order in [order_choice[0] for order_choice in AirOrder.choices]:
        request.user.prefered_air_order = air_order
        
    language = request.POST.get("language")

    if language in [lang[0] for lang in settings.LANGUAGES]:
        request.user.language = language

    # Save changes and redirect
    request.user.save()
    translation.activate(language)
    request.session["django_language"] = request.user.language
    messages.success(request, _("Settings updated."))

    return redirect("preferences")


@require_GET
def integrations(request):
    """Render the integrations settings page."""
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    
    return render(
                request, 
                "users/integrations.html",
                {
                    "order_types": order_types,
                    "selected_order_type": selected_order_type,
                    "source_choices": MediaSourceChoices.all(),
                }
            )


@require_GET
def import_data(request):
    """Render the import data settings page."""
    import_tasks = request.user.get_import_tasks()
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    return render(
        request, 
        "users/import_data.html", 
        {
            "import_tasks": import_tasks,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        }
    )


@require_GET
def export_data(request):
    """Render the export data settings page."""
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    return render(
        request, 
        "users/export_data.html",
        {
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        }
    )


@require_GET
def advanced(request):
    """Render the advanced settings page."""
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    return render(
        request, 
        "users/advanced.html",
        {
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        }
    )


@require_GET
def about(request):
    """Render the about page."""
    order_types = [list(choice) for choice in AirOrder.choices]
    
    selected_order_type = (
        request.user.last_order_type
        or request.user.prefered_air_order
    )
    return render(
        request, 
        "users/about.html", 
        {
            "version": settings.VERSION,
            "order_types": order_types,
            "selected_order_type": selected_order_type,
            "source_choices": MediaSourceChoices.all(),
        }
    )


@require_POST
def delete_import_schedule(request):
    """Delete an import schedule."""
    task_name = request.POST.get("task_name")
    try:
        task = PeriodicTask.objects.get(
            name=task_name,
            kwargs__contains=f'"user_id": {request.user.id}',
        )
        task.delete()
        messages.success(request, _("Import schedule deleted."))
    except PeriodicTask.DoesNotExist:
        messages.error(request, _("Import schedule not found."))
    return redirect("import_data")


@require_POST
def regenerate_token(request):
    """Regenerate the token for the user."""
    while True:
        try:
            request.user.regenerate_token()
            messages.success(request, _("Token regenerated successfully."))
            break
        except IntegrityError:
            continue
    return redirect("integrations")


@require_POST
def update_plex_usernames(request):
    """Update the Plex usernames for the user."""
    usernames = request.POST.get("plex_usernames", "")

    username_list = [u.strip() for u in usernames.split(",") if u.strip()]

    seen = set()
    deduplicated_usernames = [
        u for u in username_list if not (u in seen or seen.add(u))
    ]

    # Reconstruct with comma-space separation
    cleaned_usernames = ", ".join(deduplicated_usernames)

    if cleaned_usernames != request.user.plex_usernames:
        request.user.plex_usernames = cleaned_usernames
        request.user.save(update_fields=["plex_usernames"])
        messages.success(request, _("Plex usernames updated successfully"))

    return redirect("integrations")


@require_POST
def update_jellyfin_webhook_events(request):
    """Update optional Jellyfin webhook event handling for the user."""
    request.user.jellyfin_mark_played_enabled = (
        "jellyfin_mark_played_enabled" in request.POST
    )
    request.user.jellyfin_mark_unplayed_enabled = (
        "jellyfin_mark_unplayed_enabled" in request.POST
    )
    request.user.save(
        update_fields=[
            "jellyfin_mark_played_enabled",
            "jellyfin_mark_unplayed_enabled",
        ],
    )
    messages.success(request, _("Jellyfin webhook settings updated successfully"))

    return redirect("integrations")


@require_POST
def clear_search_cache(request):
    """Clear all cached search entries."""
    deleted = cache.delete_pattern("search_*")

    messages.success(
        request,
        _("Successfully cleared %(count)s search entry(s)") % {
            "count": deleted,
        }
    )
    logger.info(
        "Successfully cleared %s search entries",
        deleted,
    )

    return redirect("advanced")

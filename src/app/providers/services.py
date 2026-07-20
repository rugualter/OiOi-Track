import logging
import time

import requests
from defusedxml import ElementTree
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from pyrate_limiter import RedisBucket
from redis import Redis
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter, LimiterSession

from app.models import MediaTypes, Sources
from app.providers import (
    bgg,
    comicvine,
    hardcover,
    igdb,
    mal,
    mangaupdates,
    manual,
    openlibrary,
    tmdb,
    tvdb,
)

logger = logging.getLogger(__name__)


def get_redis_client():
    """Return a Redis client."""
    if settings.TESTING:
        import fakeredis  # noqa: PLC0415

        return fakeredis.FakeRedis()
    return Redis.from_url(settings.REDIS_URL)


redis_db = get_redis_client()
bucket_key = f"{settings.REDIS_PREFIX}_api" if settings.REDIS_PREFIX else "api"

session = LimiterSession(
    per_second=5,
    bucket_class=RedisBucket,
    bucket_kwargs={"redis": redis_db, "bucket_key": bucket_key},
)

session.mount("http://", HTTPAdapter(max_retries=3))
session.mount("https://", HTTPAdapter(max_retries=3))

session.mount(
    "https://api.myanimelist.net/v2",
    LimiterAdapter(per_minute=30),
)
session.mount(
    "https://graphql.anilist.co",
    LimiterAdapter(per_minute=85),
)
session.mount(
    "https://api.igdb.com/v4",
    LimiterAdapter(per_second=3),
)
session.mount(
    "https://api.tvmaze.com",
    LimiterAdapter(per_second=2),
)
session.mount(
    "https://comicvine.gamespot.com/api",
    LimiterAdapter(per_hour=190),
)
session.mount(
    "https://openlibrary.org",
    LimiterAdapter(per_minute=20),
)
session.mount(
    "https://api.hardcover.app/v1/graphql",
    LimiterAdapter(per_minute=50),
)
session.mount(
    "https://boardgamegeek.com/xmlapi2",
    LimiterAdapter(per_second=2),
)


class ProviderAPIError(Exception):
    """Exception raised when a provider API fails to respond."""

    def __init__(self, provider, error, details=None):
        """Initialize the exception with the provider name."""
        self.provider = provider
        response = getattr(error, "response", None)
        self.status_code = getattr(response, "status_code", None)
        try:
            provider_label = Sources(provider).label
        except ValueError:
            provider_label = provider.title()

        error_text = getattr(response, "text", str(error))
        logger.error("%s error: %s", provider_label, error_text)

        message = _("There was an error contacting the %(provider)s API") % {
            "provider": provider_label,
        }
        if self.status_code is None:
            message += _(" (network error)")
        else:
            message += _(" (HTTP %(status_code)s)") % {
                "status_code": self.status_code,
            }
        if details:
            message += f": {details}"
        message += _(". Check the logs for more details.")
        super().__init__(message)


def raise_not_found_error(provider, media_id, media_type="item"):
    """
    Raise a 404 ProviderAPIError for when a media item is not found.

    Args:
        provider: The provider source value (e.g., Sources.COMICVINE.value)
        media_id: The media ID that was not found
        media_type: The type of media (e.g., "comic", "game", "book")
    """
    error_msg = _(
    "%(media_type)s with ID %(media_id)s not found"
    ) % {
        "media_type": media_type.capitalize(),
        "media_id": media_id,
    }
    logger.error("%s: %s", provider, error_msg)

    # Create a mock 404 error response
    mock_response = type(
        "obj",
        (object,),
        {
            "status_code": 404,
            "text": error_msg,
        },
    )()
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    raise ProviderAPIError(provider, mock_error, error_msg)


def api_request(
    provider,
    method,
    url,
    params=None,
    data=None,
    headers=None,
    response_format="json",
):
    """Make a request to the API and return the response.

    Args:
        provider: Provider identifier for error messages
        method: HTTP method ("GET" or "POST")
        url: Request URL
        params: Query params for GET, JSON body for POST
        data: Raw data for POST
        headers: Request headers
        response_format: "json" (default) or "xml" for XML parsing

    Returns:
        Parsed JSON dict or ElementTree for XML
    """
    try:
        request_kwargs = {
            "url": url,
            "headers": headers,
            "timeout": settings.REQUEST_TIMEOUT,
        }

        if method == "GET":
            request_kwargs["params"] = params
            request_func = session.get
        elif method == "POST":
            request_kwargs["data"] = data
            request_kwargs["json"] = params
            request_func = session.post

        response = request_func(**request_kwargs)
        response.raise_for_status()

        if response_format == "xml":
            return ElementTree.fromstring(response.text)
        return response.json()

    except requests.exceptions.HTTPError as error:
        error_resp = error.response
        status_code = error_resp.status_code

        # handle rate limiting
        if status_code == requests.codes.too_many_requests:
            seconds_to_wait = int(error_resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, waiting %s seconds", seconds_to_wait)
            time.sleep(seconds_to_wait + 3)
            logger.info("Retrying request")
            return api_request(
                provider,
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                response_format=response_format,
            )

        raise error from None



def get_media_metadata(
    media_type,
    media_id = None,
    source = None,
    season_numbers=None,
    episode_number=None,
    order_type=None,
    provider=None,
    season_metadata= None,
    all_providers=None,
    episodes=None,
    progress=None,
    region=None
):
    """Return the metadata for the selected media."""
    if source == Sources.MANUAL.value:
        if media_type == MediaTypes.SEASON.value:
            return manual.season(media_id, season_numbers[0])
        if media_type == MediaTypes.EPISODE.value:
            return manual.episode(media_id, season_numbers[0], episode_number)
        if media_type == "tv_with_seasons":
            media_type = MediaTypes.TV.value
        if media_type == "find_next_episode": 
            return manual.find_next_episode(progress, episodes)
        if media_type == "process_episodes":
            return manual.process_episodes(season_metadata, episodes)
        return manual.metadata(media_id, media_type)

    metadata_retrievers = {
        MediaTypes.ANIME.value: lambda: mal.anime(media_id),
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.manga(media_id)
            if source == Sources.MANGAUPDATES.value
            else mal.manga(media_id)
        ),
        MediaTypes.TV.value: lambda: (
            tmdb.tv(media_id, order_type, provider)
            if source == Sources.TMDB.value
            else tvdb.tv(media_id, order_type, provider)
        ),
        "tv_with_seasons": lambda: (
            tmdb.tv_with_seasons(media_id, season_numbers, order_type, provider)
            if source == Sources.TMDB.value
            else tvdb.tv_with_seasons(media_id, season_numbers, order_type, provider)
        ),
        "find_next_episode": lambda: (
            tmdb.find_next_episode(progress, episodes)
            if source == Sources.TMDB.value
            else tvdb.find_next_episode(progress, episodes)
        ),
        "filter_providers": lambda: (
            tmdb.filter_providers(all_providers, region, provider)
            if source == Sources.TMDB.value
            else tvdb.filter_providers(all_providers, region, provider)
        ),
        "process_episodes": lambda: (
            tmdb.process_episodes(season_metadata, episodes, order_type)
            if source == Sources.TMDB.value
            else tvdb.process_episodes(season_metadata, episodes, order_type)
        ),
        "watch_provider_regions": lambda: (
            tmdb.watch_provider_regions(provider)
            if source == Sources.TMDB.value
            else tvdb.watch_provider_regions(provider)
        ),
        "get_changed_tv_ids": lambda: (
            tmdb.tv_changes()
            if source == Sources.TMDB.value
            else tvdb.tv_changes()
        ),
        "get_changed_movie_ids": lambda: (
            tmdb.movie_changes()
            if source == Sources.TMDB.value
            else tvdb.movie_changes()
        ),
        MediaTypes.SEASON.value: lambda: (
            tmdb.tv_with_seasons(media_id, season_numbers, order_type, provider)
            if source == Sources.TMDB.value
            else tvdb.tv_with_seasons(media_id, season_numbers, order_type, provider)
        ),
        MediaTypes.EPISODE.value: lambda: (
            tmdb.episode(media_id, season_numbers[0], episode_number, order_type, provider)
            if source == Sources.TMDB.value
            else tvdb.episode(media_id, season_numbers[0], episode_number, order_type, provider)
        ),
        MediaTypes.MOVIE.value: lambda: (
            tmdb.movie(media_id, provider)
            if source == Sources.TMDB.value
            else tvdb.movie(media_id, provider)
        ),
        MediaTypes.GAME.value: lambda: igdb.game(media_id),
        MediaTypes.BOOK.value: lambda: (
            hardcover.book(media_id)
            if source == Sources.HARDCOVER.value
            else openlibrary.book(media_id)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.comic(media_id),
        MediaTypes.BOARDGAME.value: lambda: bgg.boardgame(media_id),
    }
    return metadata_retrievers[media_type]()


def search(media_type, query, page, source=None, order_type=None):
    """Search for media based on the query and return the results."""
    search_handlers = {
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.search(query, page)
            if source == Sources.MANGAUPDATES.value
            else mal.search(media_type, query, page)
        ),
        
        MediaTypes.ANIME.value: lambda: mal.search(media_type, query, page),
        MediaTypes.TV.value: lambda: (
            tmdb.search(media_type, query, page, order_type)
            if source == Sources.TMDB.value
            else tvdb.search(media_type, query, page, order_type)
        ),
        MediaTypes.MOVIE.value: lambda: (
            tmdb.search(media_type, query, page)
            if source == Sources.TMDB.value
            else tvdb.search(media_type, query, page)
        ),
        MediaTypes.SEASON.value: lambda: (
            tmdb.search(MediaTypes.TV.value, query, page, order_type)
            if source == Sources.TMDB.value
            else tvdb.search(MediaTypes.TV.value, query, page, order_type)
        ),
        MediaTypes.EPISODE.value: lambda: (
            tmdb.search(MediaTypes.TV.value, query, page, order_type)
            if source == Sources.TMDB.value
            else tvdb.search(MediaTypes.TV.value, query, page, order_type)
        ),
        MediaTypes.GAME.value: lambda: igdb.search(query, page),
        MediaTypes.BOOK.value: lambda: (
            openlibrary.search(query, page)
            if source == Sources.OPENLIBRARY.value
            else hardcover.search(query, page)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.search(query, page),
        MediaTypes.BOARDGAME.value: lambda: bgg.search(query, page),
    }
    return search_handlers[media_type]()

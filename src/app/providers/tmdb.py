import logging
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _
from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services


max_workers_seasons = 8
max_workers_episodes = 10
max_workers_lists = 10
logger = logging.getLogger(__name__)
base_url = "https://api.themoviedb.org/3"

def get_base_url_tmdb():
    return base_url

def get_tmdb_language():
    """Return the language tag to use for TMDB requests."""
    language = (
        translation.get_language()
        or settings.TMDB_LANG
        or "en-US"
    )
    
    return language.replace("-","-").split("-")[0] + ("-" + language.split("-")[1].upper() if "-" in language else "")

def tmdb_get_base_params_prefered():
    """Return the shared TMDB request parameters."""
    return {
        "api_key": settings.TMDB_API,
        "language": get_tmdb_language(),
    }
    
def tmdb_get_base_params_fallback():
    """Return the shared TMDB request parameters."""
    return {
        "api_key": settings.TMDB_API,
        "language": "en-US",
    }

def handle_error(error):
    """Handle TMDB API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.TMDB.value, error) from json_error

    # Handle authentication errors
    if status_code == requests.codes.unauthorized:
        details = error_json.get("status_message")
        if details:
            # Remove trailing period if present
            details = details.rstrip(".")
            raise services.ProviderAPIError(Sources.TMDB.value, error, details)

    raise services.ProviderAPIError(
        Sources.TMDB.value,
        error,
    )

def get_external_links(external_ids, tmdb_id=None):
    """Build external links dictionary from TMDB external_ids response."""
    links = {}

    if external_ids.get("imdb_id"):
        links["IMDb"] = f"https://www.imdb.com/title/{external_ids['imdb_id']}/"

    if external_ids.get("tvdb_id"):
        links["TVDB"] = (
            f"https://www.thetvdb.com/dereferrer/series/{external_ids['tvdb_id']}"
        )

    if external_ids.get("wikidata_id"):
        links["Wikidata"] = (
            f"https://www.wikidata.org/wiki/{external_ids['wikidata_id']}"
        )

    # Only passed in for movies as Letterboxd seldom supports TV
    if tmdb_id:
        # https://letterboxd.com/about/film-data/
        # Letterboxd will redirect to the correct movie
        # as they source their data from TMDB
        links["Letterboxd"] = f"https://www.letterboxd.com/tmdb/{tmdb_id}"

    return links

def search(media_type, query, page, order_type=None):
    """Search for media on TMDB."""
    cache_key = f"search_{Sources.TMDB.value}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/search/{media_type}"

        params_prefered = {
            **tmdb_get_base_params_prefered(),
            "query": query,
            "page": page,
        }
        
        params_fallback = {
            **tmdb_get_base_params_fallback(),
            "query": query,
            "page": page,
        }

        if settings.TMDB_NSFW:
            params_prefered["include_adult"] = "true"
            params_fallback["include_adult"] = "true"

        try:
            response_prefered = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params_prefered,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)
            
        try:
            response_fallback = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params_fallback,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        
        total_results = max(response_prefered.get("total_results"), response_fallback.get("total_results"))
        seen = set()
        merged = []

        for response in (response_prefered, response_fallback):
            for media in response.get("results", []):
                media_id = media.get("id")
                if media_id not in seen:
                    seen.add(media_id)
                    merged.append(media)

        response_prefered["results"] = merged
        response = response_prefered
        
        results = [
            {
                "media_id": media.get("id"),
                "source": Sources.TMDB.value,
                "media_type": media_type,
                "title": get_title({}, media),
                "original_title": get_title_original(media),
                "order_type": None,
                "original_language": media.get("original_language"),
                "image": get_image_url(media.get("poster_path")),
                "release_year": get_release_year(media),
            }
            for media in response["results"]
        ]
        
        if media_type == MediaTypes.TV.value:
            for result in results:
                result["order_type"] = 'official'
        
        per_page = 20  # TMDB always returns 20 results per page
        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data

def find(external_id, external_source):
    """Search for media on TMDB."""
    cache_key = f"find_{Sources.TMDB.value}_{external_id}_{external_source}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/find/{external_id}"
   
        params = {
            **tmdb_get_base_params_prefered(),
            "external_source": external_source,
        }
        

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)
            
        
        cache.set(cache_key, response)
        return response

    return data

def translated_status(status):
    """Return a translated TMDB status string."""

    translations = {
        # Movies
        "Rumored": _("Rumored"),
        "Planned": _("Planned"),
        "In Production": _("In Production"),
        "Post Production": _("Post Production"),
        "Released": _("Released"),
        "Canceled": _("Canceled"),

        # TV Series
        "Returning Series": _("Returning Series"),
        "Ended": _("Ended"),
        "Pilot": _("Pilot"),
    }

    return translations.get(status, status)

def translated_genre(genre):
    """Return a translated TMDB genre name."""

    translations = {
        # Movie genres
        "Action": _("Action"),
        "Adventure": _("Adventure"),
        "Animation": _("Animation"),
        "Comedy": _("Comedy"),
        "Crime": _("Crime"),
        "Documentary": _("Documentary"),
        "Drama": _("Drama"),
        "Family": _("Family"),
        "Fantasy": _("Fantasy"),
        "History": _("History"),
        "Horror": _("Horror"),
        "Music": _("Music"),
        "Mystery": _("Mystery"),
        "Romance": _("Romance"),
        "Science Fiction": _("Science Fiction"),
        "TV Movie": _("TV Movie"),
        "Thriller": _("Thriller"),
        "War": _("War"),
        "Western": _("Western"),

        # TV-only genres
        "Action & Adventure": _("Action & Adventure"),
        "Kids": _("Kids"),
        "News": _("News"),
        "Reality": _("Reality"),
        "Sci-Fi & Fantasy": _("Sci-Fi & Fantasy"),
        "Soap": _("Soap"),
        "Talk": _("Talk"),
        "War & Politics": _("War & Politics"),
    }

    return translations.get(genre, genre)

def get_watch_providers(media_type, media_id, provider, season_number=None):
    
    if media_type == MediaTypes.MOVIE.value:
        cache_key = f"{Sources.TMDB.value}_providers_{provider}_{MediaTypes.MOVIE.value}_{media_id}"
        data = cache.get(cache_key)
        
        if data is None:
            url = f"{base_url}/movie/{media_id}/watch/providers"
            params = {
                **tmdb_get_base_params_prefered()
            }
            
            try:
                response = services.api_request(
                    Sources.TMDB.value,
                    "GET",
                    url,
                    params=params,
                )
            except requests.exceptions.HTTPError as error:
                handle_error(error)
                
            providers = response.get("results", {})
            cache.set(cache_key, providers)
            return providers
        
        return data
            
    elif media_type == MediaTypes.TV.value:
        
        cache_key = f"{Sources.TMDB.value}_providers_{provider}_{MediaTypes.TV.value}_{media_id}"
        data = cache.get(cache_key)
        
        if data is None:
            url = f"{base_url}/tv/{media_id}/watch/providers"
            params = {
                **tmdb_get_base_params_prefered()
            }
            
            try:
                response = services.api_request(
                    Sources.TMDB.value,
                    "GET",
                    url,
                    params=params,
                )
            except requests.exceptions.HTTPError as error:
                handle_error(error)
                
            providers = response.get("results", {})
            cache.set(cache_key, providers)
            return providers
        
        return data
    
    elif media_type == MediaTypes.SEASON.value:
        
        cache_key = f"{Sources.TMDB.value}_providers_{provider}_{MediaTypes.SEASON.value}_{media_id}"
        data = cache.get(cache_key)
        
        if data is None:
            url = f"{base_url}/tv/{media_id}/season/{season_number}/watch/providers" 
            params = {
                **tmdb_get_base_params_prefered()
            }
            
            try:
                response = services.api_request(
                    Sources.TMDB.value,
                    "GET",
                    url,
                    params=params,
                )
            except requests.exceptions.HTTPError as error:
                handle_error(error)
                
            providers = response.get("results", {})
            cache.set(cache_key, providers)
            return providers
        
        return data    
        
def movie(media_id, provider):
    """Return the metadata for the selected movie from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{provider}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/movie/{media_id}"
        appends = ["recommendations", "external_ids", "credits", "translations"]
        params = {
            **tmdb_get_base_params_prefered(),
            "append_to_response": ",".join(appends),
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )

            if response.get("belongs_to_collection", {}) is not None and (
                collection_id := response.get("belongs_to_collection", {}).get("id")
            ):
                try:
                    collection_response = services.api_request(
                        Sources.TMDB.value,
                        "GET",
                        f"{base_url}/collection/{collection_id}",
                        params={**tmdb_get_base_params_prefered()},
                    )
                except requests.exceptions.HTTPError as error:
                    logger.warning("Failed to get collection: %s", error)
                    collection_response = {}
            else:
                collection_response = {}
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Filter out collection items from recommendations, to avoid duplicates
        collection_response_trans = add_collection_part_translations(collection_response)
        collection_items = get_collection(collection_response_trans)
        collection_ids = [item["media_id"] for item in collection_items if "media_id" in item]
        recommended_items = response.get("recommendations", {}).get("results", [])
        filtered_recommendations = [
            item
            for item in recommended_items
            if "id" in item and item["id"] not in collection_ids
        ]
        
        final_recommendations = add_recommendation_translations(filtered_recommendations[:8])
    

        cast = response.get("credits", {}).get("cast", [])
        filtered_cast = [
            {
                "id": member.get("id"),
                "name": member.get("name"),
                "character": member.get("character"),
                "url": f"https://www.themoviedb.org/person/{member.get("id")}",
                "image": get_image_url(member.get("profile_path")),
            }
            for member in cast[:30]
        ]
        
        providers = {}
        if provider == "tmdb":
            providers = get_watch_providers(MediaTypes.MOVIE.value, media_id, provider)

        data = {
            "media_id": media_id,
            "source": Sources.TMDB.value,
            "source_url": f"https://www.themoviedb.org/movie/{media_id}",
            "media_type": MediaTypes.MOVIE.value,
            "title": get_title(response.get("translations"), response),
            "original_title": get_title_original(response),
            "original_language": response.get("original_language"),
            "max_progress": 1,
            "order_type": None,
            "image": get_image_url(response.get("poster_path")),
            "synopsis": get_synopsis(response.get("translations"), response),
            "genres": get_genres(response.get("genres")),
            "score": get_score(response.get("vote_average")),
            "score_count": response.get("vote_count"),
            "release_year": get_release_year(response),
            "details": {
                "format": _("Movie"),
                "release_date": get_start_date(response.get("release_date")),
                "status": translated_status(response.get("status")),
                "runtime": get_readable_duration(response.get("runtime")),
                "studios": get_companies(response.get("production_companies")),
                "country": get_country(response.get("production_countries")),
                "languages": get_languages(response.get("spoken_languages")),
            },
            "cast": filtered_cast,
            "total_cast_count": len(cast),
            "related": {
                collection_response_trans.get("name", "collection"): collection_items,
                "recommendations": get_related(
                    final_recommendations,
                    MediaTypes.MOVIE.value,
                ),
            },
            "external_links": get_external_links(
                response.get("external_ids", {}), media_id
            ),
            "providers": providers,
        }

        cache.set(cache_key, data)

    return data

def get_cached_seasons(media_id, season_numbers, order_type = None, provider = None):
    """Check cache for seasons and return cached data and list of uncached seasons."""
    cached_data = {}
    uncached_seasons = []

    for season_number in season_numbers:
        season_cache_key = (
            f"{Sources.TMDB.value}_{provider}_{MediaTypes.SEASON.value}_{media_id}_{order_type}_{season_number}"
        )
        season_data = cache.get(season_cache_key)
        if season_data:
            cached_data[f"season/{season_number}"] = season_data
        else:
            uncached_seasons.append(season_number)

    return cached_data, uncached_seasons

def enrich_season_with_tv_data(season_data, tv_data, media_id, season_number):

    """Add TV show metadata to season metadata."""
    season_data["media_id"] = media_id
    season_data["source_url"] = (
        f"https://www.themoviedb.org/tv/{media_id}/season/{season_number}"
    )
    season_data["title"] = tv_data["title"]
    season_data["original_title"] = tv_data["original_title"]
    season_data["original_language"] = tv_data["original_language"]
    season_data["tvdb_id"] = tv_data["tvdb_id"]
    season_data["external_links"] = tv_data["external_links"]
    season_data["genres"] = tv_data["genres"]
    season_data["release_year"] = tv_data["release_year"]
    if season_data["synopsis"] == _("No synopsis available."):
        season_data["synopsis"] = tv_data["synopsis"]
        
    return season_data

def fetch_and_cache_seasons(media_id, season_numbers, tv_data, order_type=None, provider = None):
    """Fetch uncached seasons from API and cache them."""
    url = f"{base_url}/tv/{media_id}"
    base_append = "recommendations,external_ids,translations"
    max_seasons_per_request = 8
    fetched_tv_data = tv_data
    result_data = {}
    
    season_subsets = [
        season_numbers[i : i + max_seasons_per_request]
        for i in range(0, len(season_numbers), max_seasons_per_request)
    ]

    
    def fetch_subset(season_subset, flag):
        errors = []
        append_text = ",".join(
            [
                f"season/{season},season/{season}/translations"
                for season in season_subset
            ]
        )

        params = {
            **tmdb_get_base_params_prefered(),
            "append_to_response": f"{base_append},{append_text}",
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
            
        except requests.exceptions.HTTPError as error:
            errors.append(error)
            return None, None, errors
        # Cache TV metadata if we haven't fetched it yet
        
        batch_tv_data = None
        
        if flag:
            tv_cache_key = f"{Sources.TMDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}_{order_type}"
            batch_tv_data  = process_tv(response, order_type, provider)
            fetched_tv_data = batch_tv_data
            cache.set(tv_cache_key, batch_tv_data)

        batch_result = {}
        
        # Process and cache each season
        for season_number in season_subset:
            season_key = f"season/{season_number}"
            if season_key not in response:
                not_found_response = requests.Response()
                not_found_response.status_code = 404
                not_found_error = type("Error", (), {"response": not_found_response})
                errors.append(not_found_error)

            season_data = process_season(
                response[season_key], provider, response.get(f"{season_key}/translations", {}), media_id, order_type
            )

            season_data = enrich_season_with_tv_data(
                season_data,
                fetched_tv_data,
                media_id,
                season_number,
            )
            
            #now we need to add the episodes translations so inside season

            cache.set(
                f"{Sources.TMDB.value}_{provider}_{MediaTypes.SEASON.value}_{media_id}_{season_number}",
                season_data,
            )
            
            batch_result[season_key] = season_data

        return batch_result, batch_tv_data, errors
    
    errors = []
    
    with ThreadPoolExecutor(max_workers=min(max_workers_seasons, len(season_subsets)+1)) as executor:
        futures = [
            executor.submit(fetch_subset, subset, index == 0)
            for index, subset in enumerate(season_subsets)
        ]

        for future in as_completed(futures):
            try:
                batch_result, batch_tv_data, batch_errors = future.result()

                if batch_result is not None:
                    result_data.update(batch_result)

                if batch_tv_data is not None:
                    fetched_tv_data = batch_tv_data
                
                errors.extend(batch_errors)   

            except Exception as error:
                errors.append(error)

    result_data = dict(
        sorted(
            result_data.items(),
            key=lambda item: int(item[1]["season_number"]),
        )
    )
        
    if errors:
        handle_error(errors[0])

    return result_data, fetched_tv_data
    
def tv_with_seasons(media_id, season_numbers, order_type=None, provider = None):
    """Return the metadata for the tv show with seasons appended to the response."""
    if not season_numbers:
        return tv(media_id, order_type)

    tv_cache_key = f"{Sources.TMDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}"
    tv_data = cache.get(tv_cache_key)

    cached_seasons, uncached_seasons = get_cached_seasons(media_id, season_numbers, order_type, provider)

    if tv_data is None and not uncached_seasons:
        tv_data = tv(media_id, order_type)

    if uncached_seasons:
        fetched_seasons, fetched_tv_data = fetch_and_cache_seasons(
            media_id,
            uncached_seasons,
            tv_data,
            order_type,
            provider,
        )

        if tv_data is None:
            tv_data = fetched_tv_data

        cached_seasons.update(fetched_seasons)

    cached_seasons = dict(
        sorted(
            cached_seasons.items(),
            key=lambda item: int(item[1]["season_number"])
        )
    )
    
    return tv_data | cached_seasons

def tv(media_id, order_type=None, provider = None):
    """Return the metadata for the selected tv show from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}_{order_type}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/tv/{media_id}"
        params = {
            **tmdb_get_base_params_prefered(),
            "append_to_response": "recommendations,external_ids,translations",
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        data = process_tv(response, order_type, provider)
        cache.set(cache_key, data)

    return data

def process_tv(response, order_type=None, provider = None):
    """Process the metadata for the selected tv show from The Movie Database."""
    num_episodes = response.get("number_of_episodes")
    next_episode = response.get("next_episode_to_air")
    last_episode = response.get("last_episode_to_air")
    
    new_seasons = add_season_translations(response.get("id"), response.get("seasons", []))
    new_seasons.sort(key=lambda season: int(season.get("season_number",0)))
    recommendations = response.get("recommendations", {}).get("results", [])
    recommendations = add_recommendation_translations(recommendations[:8])
    
    providers = {}
    if provider == "tmdb":
        providers = get_watch_providers(MediaTypes.MOVIE.value, response.get("id"), provider)
    
    return {
        "media_id": response.get("id"),
        "source": Sources.TMDB.value,
        "source_url": f"https://www.themoviedb.org/tv/{response.get("id")}",
        "media_type": MediaTypes.TV.value,
        "title": get_title(response.get("translations"), response),
        "original_title": get_title_original(response),
        "original_language": response.get("original_language"),
        "release_year": get_release_year(response),
        "max_progress": num_episodes,
        "order_type": 'official',
        "image": get_image_url(response.get("poster_path")),
        "synopsis": get_synopsis(response.get("translations"), response),
        "genres": get_genres(response.get("genres")),
        "score": get_score(response.get("vote_average")),
        "score_count": response.get("vote_count"),
        "details": {
            "format": _("TV"),
            "first_air_date": get_start_date(response.get("first_air_date")),
            "last_air_date": response.get("last_air_date"),
            "status": translated_status(response.get("status")),
            "seasons": response.get("number_of_seasons"),
            "episodes": num_episodes,
            "runtime": get_runtime_tv(response.get("episode_run_time")),
            "studios": get_companies(response.get("production_companies")),
            "country": get_country(response.get("production_countries")),
            "languages": get_languages(response.get("spoken_languages")),
        },
        "related": {
            "seasons": get_related(
                new_seasons,
                MediaTypes.SEASON.value,
                response,
            ),
            "recommendations": get_related(
                recommendations,
                MediaTypes.TV.value,
            ),
        },
        "tvdb_id": response.get("external_ids", {}).get("tvdb_id"),
        "external_links": get_external_links(response.get("external_ids", {})),
        "last_episode_season": last_episode["season_number"] if last_episode else None,
        "next_episode_season": next_episode["season_number"] if next_episode else None,
        "providers": providers,
    }

def fetch_collection_part_translation(media_id, media_type):
    
    cache_key = (
        f"tmdb_translation_{media_type}_{media_id}"
        f"_{get_tmdb_language()}"
    )
    
    cached = cache.get(cache_key)
    
    if cached is not None:
        return media_id, cached
    
    url = f"{base_url}/{media_type}/{media_id}"

    params = {
        **tmdb_get_base_params_prefered(),
        "append_to_response": "translations",
    }

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            url,
            params=params,
        )
        
        translations = response.get(
            "translations",
            {}
        )
        
        cache.set(
            cache_key,
            translations,
            60 * 60 * 24 * 30,
        )

        return media_id, translations

    except requests.exceptions.HTTPError:
        return media_id, {}

def add_collection_part_translations(collection_response):
    parts = collection_response.get("parts", [])

    with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(parts)+1)) as executor:
        futures = [
            executor.submit(
                fetch_collection_part_translation,
                part["id"],
                part.get("media_type", "movie"),
            )
            for part in parts
        ]

        translations = {}

        for future in as_completed(futures):
            media_id, data = future.result()
            translations[media_id] = data

    for part in parts:
        part["translations"] = translations.get(
            part["id"],
            {}
        )
        
    return collection_response

def fetch_recommendation_translation(media_id, media_type):
    
    cache_key = (
        f"tmdb_translation_{media_type}_{media_id}"
        f"_{get_tmdb_language()}"
    )
    
    cached = cache.get(cache_key)
    
    if cached is not None:
        return media_id, cached
    
    url = f"{base_url}/{media_type}/{media_id}"

    params = {
        **tmdb_get_base_params_prefered(),
        "append_to_response": "translations",
    }

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            url,
            params=params,
        )
        
        translations = response.get(
            "translations",
            {}
        )
        
        cache.set(
            cache_key,
            translations,
            60 * 60 * 24 * 30,
        )

        return media_id, translations

    except requests.exceptions.HTTPError:
        return media_id, {}

def add_recommendation_translations(recommendations):

    with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(recommendations)+1)) as executor:
        futures = [
            executor.submit(
                fetch_recommendation_translation,
                recommendation["id"],
                recommendation["media_type"],
            )
            for recommendation in recommendations
        ]

        translations = {}

        for future in as_completed(futures):
            media_id, data = future.result()
            translations[media_id] = data

    for recommendation in recommendations:
        recommendation["translations"] = translations.get(
            recommendation["id"],
            {}
        )

    return recommendations

def fetch_season_translation(media_id, season_number):
    
    cache_key = (
        f"tmdb_season_translation_{season_number}_{media_id}"
        f"_{get_tmdb_language()}"
    )
    
    cached = cache.get(cache_key)
    
    if cached is not None:
        return season_number, cached
    
    url = (
        f"{base_url}/tv/{media_id}/season/{season_number}"
    )

    params = {
        **tmdb_get_base_params_prefered(),
        "append_to_response": "translations",
    }

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            url,
            params=params,
        )
        
        translations = response.get(
            "translations",
            {}
        )
        
        cache.set(
            cache_key,
            translations,
            60 * 60 * 24 * 30,
        )

        return season_number, translations

    except requests.exceptions.HTTPError:
        return season_number, {}

def add_season_translations(media_id, seasons):

    with ThreadPoolExecutor(max_workers=min(max_workers_seasons, len(seasons)+1)) as executor:
        futures = [
            executor.submit(
                fetch_season_translation,
                media_id,
                season["season_number"],
            )
            for season in seasons
        ]

        translations = {}

        for future in as_completed(futures):
            season_number, data = future.result()
            translations[season_number] = data

    for season in seasons:
        season["translations"] = translations.get(
            season["season_number"],
            {}
        )

    return seasons

def fetch_episode_translation(media_id, season_number, episode_number):
    
    cache_key = (
        f"tmdb_episode_translation_{season_number}_{episode_number}_{media_id}"
        f"_{get_tmdb_language()}"
    )
    
    cached = cache.get(cache_key)
    if cached is not None:
        return episode_number, cached
    
    url = (
        f"{base_url}/tv/{media_id}/season/"
        f"{season_number}/episode/{episode_number}"
    )

    params = {
        **tmdb_get_base_params_prefered(),
        "append_to_response": "translations",
    }

    try:
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            url,
            params=params,
        )
        
        translations = response.get(
            "translations",
            {}
        )
        
        cache.set(
            cache_key,
            translations,
            60 * 60 * 24 * 30,
        )

        return episode_number, translations

    except requests.exceptions.HTTPError:
        return episode_number, {}

def add_episode_translations(media_id, season_number, episodes):

    with ThreadPoolExecutor(max_workers=min(max_workers_episodes, len(episodes)+1)) as executor:
        futures = [
            executor.submit(
                fetch_episode_translation,
                media_id,
                season_number,
                episode["episode_number"],
            )
            for episode in episodes
        ]

        translations = {}

        for future in as_completed(futures):
            episode_number, data = future.result()
            translations[episode_number] = data

    for episode in episodes:
        episode["translations"] = translations.get(
            episode["episode_number"],
            {}
        )

    return episodes

def process_season(response, provider, translations, media_id, order_type=None):
    """Process the metadata for the selected season from The Movie Database."""
    episodes = response.get("episodes")
    num_episodes = len(episodes)

    runtimes = []
    total_runtime = 0
    score_count = 0

    for episode in episodes:
        if episode.get("runtime") is not None:
            runtimes.append(episode["runtime"])
            total_runtime += episode["runtime"]
        score_count += episode["vote_count"]

    avg_runtime = (
        get_readable_duration(sum(runtimes) / len(runtimes)) if runtimes else None
    )
    total_runtime = get_readable_duration(total_runtime) if total_runtime else None
    
    new_epp = add_episode_translations(media_id, response.get("season_number"), response.get("episodes", []))
    new_epp.sort(key=lambda episode: int(episode["episode_number"]))
    
    providers = {}
    if provider == "tmdb":
        providers = get_watch_providers(MediaTypes.SEASON.value, media_id, provider, response.get("season_number"))
    
    return {
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": get_season_episode_title(translations, response),
        "season_original_title": get_season_episode_title_original(translations, response),
        "season_release_year": get_release_year(response),
        "max_progress": episodes[-1]["episode_number"] if episodes else 0,
        "image": get_image_url(response.get("poster_path")),
        "season_number": response.get("season_number"),
        "order_type": 'official',
        "synopsis": get_synopsis(translations, response),
        "score": get_score(response.get("vote_average")),
        "score_count": score_count,
        "details": {
            "first_air_date": get_start_date(response.get("air_date")),
            "last_air_date": get_end_date(response),
            "episodes": num_episodes,
            "runtime": avg_runtime,
            "total_runtime": total_runtime,
        },
        "episodes": new_epp,
        "providers": providers,
    }

def get_format(media_type):
    """Return media_type capitalized."""
    if media_type == MediaTypes.TV.value:
        return _("TV")
    return _("Movie")

def get_image_url(path):
    """Return the image URL for the media."""
    # when no image, value from response is null
    # e.g movie: 445290
    if path:
        return f"https://image.tmdb.org/t/p/w500{path}"
    return settings.IMG_NONE

def get_title(translations_set, original):
    """Return the localized title for a movie or TV show.

    Fallback order:
    1. Preferred language (e.g. pt-PT)
    2. en-US
    3. Any English translation (iso_639_1 == "en")
    4. Original title/name
    """
    preferred = get_tmdb_language()  # e.g. "pt-PT"
    fallback = "en-US"

    def split_language(language):
        lang, region = language.split("-")
        return lang, region
    
    preferred_lang, preferred_region = split_language(preferred)
    fallback_lang, fallback_region = split_language(fallback)

    translations = translations_set.get("translations", [])
    
    def find_translation(lang, region=None):
        for translation in translations:
            if translation.get("iso_639_1") != lang:
                continue

            if region and translation.get("iso_3166_1") != region:
                continue

            data = translation.get("data", {})
            title = data.get("title") or data.get("name")

            if title:
                return title

        return None
    
     # 1. Preferred language/region (pt-PT)
    title = find_translation(preferred_lang, preferred_region)
    if title:
        return title

    # 2. Exact English locale (en-US)
    title = find_translation(fallback_lang, fallback_region)
    if title:
        return title

    # 3. Any English translation
    title = find_translation(fallback_lang)
    if title:
        return title
    
    return original.get("title") or original.get("name")

def get_season_episode_title(translations_set, response):
    """Return the localized title for a movie or TV show.

    Fallback order:
    1. Preferred language (e.g. pt-PT)
    2. en-US
    3. Any English translation (iso_639_1 == "en")
    4. Original title/name
    """
    preferred = "en-US"  # e.g. "pt-PT"
    fallback = "en-US"

    def split_language(language):
        lang, region = language.split("-")
        return lang, region
    
    preferred_lang, preferred_region = split_language(preferred)
    fallback_lang, fallback_region = split_language(fallback)

    translations = translations_set.get("translations", [])
    
    def find_translation(lang, region=None):
        for translation in translations:
            if translation.get("iso_639_1") != lang:
                continue

            if region and translation.get("iso_3166_1") != region:
                continue

            data = translation.get("data", {})
            title = data.get("title") or data.get("name")

            if title:
                return title

        return None
    
     # 1. Preferred language/region (pt-PT)
    title = find_translation(preferred_lang, preferred_region)
    if title:
        return title

    # 2. Exact English locale (en-US)
    title = find_translation(fallback_lang, fallback_region)
    if title:
        return title

    # 3. Any English translation
    title = find_translation(fallback_lang)
    if title:
        return title
    
    return response.get("title") or response.get("name")

def get_season_episode_title_original(translations_set, response):
    """Return the localized title for a movie or TV show.

    Fallback order:
    1. Preferred language (e.g. pt-PT)
    2. en-US
    3. Any English translation (iso_639_1 == "en")
    4. Original title/name
    """
    preferred = "en-US"  # e.g. "pt-PT"
    fallback = "en-US"

    def split_language(language):
        lang, region = language.split("-")
        return lang, region
    
    preferred_lang, preferred_region = split_language(preferred)
    fallback_lang, fallback_region = split_language(fallback)

    translations = translations_set.get("translations", [])
    
    def find_translation(lang, region=None):
        for translation in translations:
            if translation.get("iso_639_1") != lang:
                continue

            if region and translation.get("iso_3166_1") != region:
                continue

            data = translation.get("data", {})
            title = data.get("title") or data.get("name")

            if title:
                return title

        return None
    
     # 1. Preferred language/region (pt-PT)
    title = find_translation(preferred_lang, preferred_region)
    if title:
        return title

    # 2. Exact English locale (en-US)
    title = find_translation(fallback_lang, fallback_region)
    if title:
        return title

    # 3. Any English translation
    title = find_translation(fallback_lang)
    if title:
        return title
    
    return response.get("title") or response.get("name")

def get_title_original(response):
    """Return the title for the media."""
    # tv shows have name instead of title
    return response.get("original_title") or response.get("original_name") or response.get("title") or response.get("name") 

def get_release_year(response):
    """Return the release year for the media."""
    # tv shows have first_air_date instead of release_date
    date_str = response.get("release_date") or response.get("first_air_date") or response.get("air_date")

    if date_str:
        try:
            return int(date_str[:4])
        except ValueError:
            pass

    return None

def get_start_date(date):
    """Return the start date for the media."""
    # when unknown date, value from response is empty string
    # e.g movie: 445290
    if date == "":
        return None
    return date

def get_end_date(response):
    """Return the latest air date for the season."""
    dates = [
        ep["air_date"]
        for ep in response.get("episodes", [])
        if ep.get("air_date")
    ]
    return max(dates, default=None)

def get_synopsis(response, original):
    """Return the synopsis for the media."""
    # when unknown synopsis, value from response is empty string
    # e.g movie: 445290
    
    preferred = get_tmdb_language()  # e.g. "pt-PT"
    fallback = "en-US"

    def split_language(language):
        lang, region = language.split("-")
        return lang, region
    
    preferred_lang, preferred_region = split_language(preferred)
    fallback_lang, fallback_region = split_language(fallback)

    translations = response.get("translations", [])
    
    def find_translation(lang, region=None):
        for translation in translations:
            if translation.get("iso_639_1") != lang:
                continue

            if region and translation.get("iso_3166_1") != region:
                continue

            data = translation.get("data", {})
            overview = data.get("overview")

            if overview:
                return overview

        return None
    
     # 1. Preferred language/region (pt-PT)
    overview = find_translation(preferred_lang, preferred_region)
    if overview:
        return overview

    # 2. Exact English locale (en-US)
    overview = find_translation(fallback_lang, fallback_region)
    if overview:
        return overview

    # 3. Any English translation
    overview = find_translation(fallback_lang)
    if overview:
        return overview
    
    if original.get("overview") == "":
        return _("No synopsis available.")
    
    return original.get("overview")
    
def get_readable_duration(duration):
    """Convert duration in minutes to a readable format."""
    # if unknown movie runtime, value from response is 0
    # e.g movie: 274613
    if duration:
        hours, minutes = divmod(int(duration), 60)
        return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    return None

def get_runtime_tv(runtime):
    """Return the runtime for the tv show."""
    # when unknown runtime, value from response is empty list
    # e.g: tv:66672
    if runtime:
        return get_readable_duration(runtime[0])
    return None

def season_scores_count(response):
    """Return the scores count for the season."""
    return sum(
        episode.get("vote_count", 0)
        for episode in response.get("episodes", [])
    )

def get_genres(genres):
    """Return the genres for the media."""
    if not genres:
        return None

    return [translated_genre(genre["name"]) for genre in genres if "name" in genre]

def get_country(countries):
    """Return the production country for the media."""
    if not countries:
        return None

    return countries[0].get("name")

def get_languages(languages):
    """Return the languages for the media."""
    if not languages:
        return None

    return [
        language["name"]
        for language in languages
        if "name" in language
    ]

def get_companies(companies):
    """Return the production companies for the media."""
    if not companies:
        return None

    return [
        company["name"]
        for company in companies[:3]
        if "name" in company
    ]

def get_score(score):
    """Return the score for the media with one decimal place."""
    # when unknown score, value from response is 0.0

    return round(score, 1)

def get_related(related_medias, media_type, parent_response=None):
    """Return list of related media for the selected media."""
    related = []
    for media in related_medias:
        data = {
            "source": Sources.TMDB.value,
            "media_type": media_type,
            "image": get_image_url(media.get("poster_path")),
        }
        if media_type == MediaTypes.MOVIE.value:
             data["order_type"] = None
        
        if media_type == MediaTypes.TV.value or media_type == MediaTypes.EPISODE.value:
             data["order_type"] = 'official'    
            
        if media_type == MediaTypes.SEASON.value:
            data["media_id"] = parent_response["id"]
            data["title"] = get_title(parent_response.get("translations"), parent_response)
            data["original_title"] = get_title_original(parent_response)
            data["release_year"] = get_release_year(parent_response)
            data["season_number"] = media.get("season_number")
            data["order_type"] = 'official'
            data["season_title"] = get_season_episode_title(media.get("translations"), media)
            data["season_original_title"] = get_season_episode_title_original(media.get("translations"), media)
            data["first_air_date"] = get_start_date(media.get("air_date"))
            data["season_release_year"] = get_release_year(media)
            data["max_progress"] = media.get("episode_count")
        else:
            data["media_id"] = media.get("id")
            data["title"] = get_title(media.get("translations"), media)
            data["original_title"] = get_title_original(media)
            data["release_year"] = get_release_year(media)
        related.append(data)
    
    if media_type == MediaTypes.SEASON.value:
        related.sort(key=lambda media: int(media.get("season_number", 0)))    
    
    return related

def get_collection(collection_response):
    """Format media collection list to match related media."""

    def date_key(media):
        date = media.get("release_date") or media.get("first_air_date") or media.get("air_date")
        if date is None or date == "":
            # If release date is unknown, sort by title after known releases
            title = get_title(media)
            date = f"9999-99-99-{title}"
        return date

    parts = sorted(collection_response.get("parts", []), key=date_key)
    return [
        {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "image": get_image_url(media.get("poster_path")),
            "media_id": media.get("id"),
            "order_type": None,
            "title": get_title(media.get("translations"), media),
            "original_title": get_title_original(media),
            "original_language": media.get("original_language"),
            "release_year": get_release_year(media),
        }
        for media in parts
    ]

def filter_providers(all_providers, region, provider):
    """Filter watch providers by region."""

    if provider == "tmdb":
    
        if region == "":
            return None

        if not all_providers:
            return []

        # Create a dict to get rid of duplicates across different provider types
        region_providers = all_providers.get(region, {})
        flatrate_providers = region_providers.get("flatrate", [])
        free_providers = region_providers.get("free", [])
        providers = {}
        for provider in [*flatrate_providers, *free_providers]:
            providers[provider.get("provider_id")] = provider

        # Convert dict back to list and add image URLs
        providers = list(providers.values())
        for provider in providers:
            provider["image"] = get_image_url(provider.get("logo_path"))

        providers.sort(key=lambda e: e.get("display_priority", 999))
        return providers
    
    else:
        return None

def process_episodes(season_metadata, episodes_in_db, order_type=None):
    """Process the episodes for the selected season."""
    episodes_metadata = []

    # Convert the queryset to a dictionary for efficient lookups
    tracked_episodes = {}
    for ep in episodes_in_db:
        episode_number = ep.item.episode_number
        if episode_number not in tracked_episodes:
            tracked_episodes[episode_number] = []
        tracked_episodes[episode_number].append(ep)

    for episode in season_metadata.get("episodes"):
        episode_number = episode.get("episode_number")
        episodes_metadata.append(
            {
                "media_id": season_metadata.get("media_id"),
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TMDB.value,
                "season_number": season_metadata.get("season_number"),
                "episode_number": episode_number,
                "order_type": 'official',
                "air_date": episode.get("air_date"),  # when unknown, response returns null
                "episode_release_year": get_release_year(episode),
                "image": get_image_url(episode.get("still_path")),
                "title": get_season_episode_title(episode.get("translations", {}), episode),
                "original_title": get_season_episode_title_original(episode.get("translations", {}), episode),  ##again no original title
                "overview": get_synopsis(episode.get("translations", {}), episode),
                "history": tracked_episodes.get(episode_number, []),
                "runtime": get_readable_duration(episode.get("runtime")),
                "runtime_minutes": episode.get("runtime"),
            },
        )
        
    episodes_metadata.sort(key=lambda episode: int(episode.get("episode_number", 0)))    
    
    return episodes_metadata

def find_next_episode(episode_number, episodes_metadata):
    """Find the next episode number."""
    # Find the current episode in the sorted list
    current_episode_index = None
    for index, episode in enumerate(episodes_metadata):
        if episode.get("episode_number") == episode_number:
            current_episode_index = index
            break

    # If episode not found or it's the last episode, return None
    if current_episode_index is None or current_episode_index + 1 >= len(
        episodes_metadata,
    ):
        return None

    if episodes_metadata:
        return episodes_metadata[current_episode_index + 1]["episode_number"]
    # Return the next episode number
    return None

def episode(media_id, season_number, episode_number, order_type=None, provider = None):
    """Return the metadata for the selected episode from The Movie Database."""
    tv_metadata = tv_with_seasons(media_id, [season_number], order_type=None, provider = None)
    season_metadata = tv_metadata[f"season/{season_number}"]

    for episode in season_metadata.get("episodes"):
        if episode.get("episode_number") == int(episode_number):
            return {
                "title": season_metadata.get("title"),
                "original_title": season_metadata.get("original_title"),
                "season_title": season_metadata.get("season_title"),
                "season_original_title": season_metadata.get("season_original_title"),
                "season_release_year": season_metadata.get("season_release_year"),
                "order_type": 'official',
                "episode_title": get_season_episode_title(episode.get("translations", {}), episode),
                "episode_original_title": get_season_episode_title_original(episode.get("translations", {}), episode),
                "air_date": episode.get("air_date"),
                "episode_release_year": get_release_year(episode),
                "image": get_image_url(episode.get("still_path")),
            }

    # Episode not found - throw ProviderAPIError
    msg = (
        f"Episode {episode_number} not found in season {season_number} "
        f"for {Sources.TMDB.label} with ID {media_id}"
    )
    # Create a new response object with 404 status
    not_found_response = requests.Response()
    not_found_response.status_code = 404
    # Set the error attribute to match what ProviderAPIError expects
    not_found_error = type("Error", (), {"response": not_found_response})
    raise services.ProviderAPIError(
        Sources.TMDB.value,
        error=not_found_error,
        details=msg,
    )

def watch_provider_regions(provider):
    """Return the available watch provider regions from The Movie Database."""
    if provider == "tmdb":
        cache_key = f"{Sources.TMDB.value}_provider_watch_provider_regions"
        data = cache.get(cache_key)

        if data is None:
            url = f"{base_url}/watch/providers/regions"
            params = {**tmdb_get_base_params_prefered()}

            try:
                response = services.api_request(
                    Sources.TMDB.value,
                    "GET",
                    url,
                    params=params,
                )
            except requests.exceptions.HTTPError as error:
                handle_error(error)

            data = [("", "Disabled")]
            regions = response.get("results", [])
            for region in sorted(regions, key=lambda r: r.get("name", "")):
                key = region.get("iso_3166_1")
                name = region.get("name")
                if key:
                    if not name:
                        name = key
                    data.append((key, name))

            cache.set(cache_key, data)
            
        else:
            data = [("", "Disabled")]

    return data

def get_changed_ids(media_type):
    """Return changed TMDB ids for the given media type over the last days."""
    url = f"{base_url}/{media_type}/changes"
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=3)
    changed_ids = set()
    page = 1

    while True:
        params = {
            **tmdb_get_base_params_prefered(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "page": page,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        changed_ids.update(str(result.get("id")) for result in response.get("results", []) if "id" in result)

        total_pages = response.get("total_pages", 1)
        if page >= total_pages:
            break
        page += 1

    return changed_ids

def tv_changes():
    """Return changed TV ids from TMDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.TV.value)

def movie_changes():
    """Return changed movie ids from TMDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.MOVIE.value)

import logging
from datetime import timedelta, date
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import requests
import pycountry
from babel import Locale
import tvdb_v4_official
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _
from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services, tmdb

max_workers_seasons = 8
max_workers_episodes = 10
max_workers_lists = 10

TVDB_LANGUAGE_OVERRIDES = {
    "pt-BR": "pt",
    "pt-PT": "por",
    "zh-CN": "zho",
    "zh-TW": "zhtw",
    "zh-HK": "yue",
}

logger = logging.getLogger(__name__)
tvdb = tvdb_v4_official.TVDB(settings.TVDB_API)
tvdb_base_url = "https://api4.thetvdb.com/v4"



def get_tvdb_language():
    """Return the language code to use for TVDB."""
    language = (
        translation.get_language()
        or settings.TVDB_LANG
        or "en-US"
    )

    language = language.replace("-","-").split("-")[0] + ("-" + language.split("-")[1].upper() if "-" in language else "")

    # TVDB-specific language codes
    if language in TVDB_LANGUAGE_OVERRIDES:
        return TVDB_LANGUAGE_OVERRIDES[language]

    # Convert ISO639-1 -> ISO639-2/T
    lang = pycountry.languages.get(alpha_2=language.split("-")[0])

    if lang and hasattr(lang, "alpha_3"):
        return lang.alpha_3

    return "eng"

def get_tvdb_token():
    tvdb_token = cache.get("tvdb_token")
    tvdb_token_time = cache.get("tvdb_token_time")
    # TVDB tokens last 24 hours
    if tvdb_token and (time.time() - tvdb_token_time) < 86400:
        return tvdb_token

    response = requests.post(
        f"{tvdb_base_url}/login",
        json={
            "apikey": settings.TVDB_API,
        },
        timeout=10,
    )

    response.raise_for_status()

    tvdb_token = response.json()["data"]["token"]
    tvdb_token_time = time.time()

    cache.set("tvdb_token", tvdb_token)
    cache.set("tvdb_token_time", tvdb_token_time)

    return tvdb_token

def tvdb_api_request(endpoint, params=None):
    """Make authenticated TVDB v4 API request."""

    token = get_tvdb_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    response = requests.get(
        f"{tvdb_base_url}{endpoint}",
        headers=headers,
        params=params,
        timeout=10,
    )

    response.raise_for_status()

    return response.json()

def clean_tvdb_id(value):
    """Remove TVDB type prefixes from IDs."""
    if not value:
        return None

    for prefix in ("series-", "movie-"):
        if value.startswith(prefix):
            return value[len(prefix):]

    return value

def handle_error(error):
    """Handle TVDB API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.TVDB.value, error) from json_error

    # Handle authentication errors
    if status_code == requests.codes.unauthorized:
        details = error_json.get("status_message")
        if details:
            # Remove trailing period if present
            details = details.rstrip(".")
            raise services.ProviderAPIError(Sources.TVDB.value, error, details)

    raise services.ProviderAPIError(
        Sources.TVDB.value,
        error,
    )

def get_external_links(external_ids, media_type):
    """Build external links dictionary from TVDB remoteIds."""
    links = {}
    if not external_ids:
        return links
    for member in external_ids:
        source = member.get("sourceName")
        rid = member.get("id")
        if source == "IMDB":
            links["IMDb"] = f"https://www.imdb.com/title/{rid}/"
        elif source == "TheMovieDB.com":
            links["Letterboxd"] = f"https://www.letterboxd.com/tmdb/{rid}"
            if media_type == MediaTypes.TV.value:
                links["TMDB"] = f"https://www.themoviedb.org/tv/{rid}/"
            else:
                links["TMDB"] = f"https://www.themoviedb.org/movie/{rid}/"
        elif source == "Wikidata":
            links["Wikidata"] = f"https://www.wikidata.org/wiki/{rid}"
        elif source == "Wikipedia":
            links["Wikipedia"] = f"https://en.wikipedia.org/wiki/{rid}"
        elif source == "EIDR":
            links["EIDR"] = f"https://ui.eidr.org/view/content?id={rid}"
    return links

def native_language_name(alpha3):
    lang = pycountry.languages.get(alpha_3=alpha3)
    if not lang or not hasattr(lang, "alpha_2"):
        return None

    code = lang.alpha_2
    locale = Locale.parse(code)
    return locale.languages[code]

def get_translation(translations, language, original, type):
    """
    Return a translated field from a list of translation objects.
    This matches the logic from tvdb_old.py.
    """
    if not translations:
        return None

    if type == "search":
        value = translations.get(language)
        if value:
            return value

        # English fallback
        value = translations.get("eng")
        if value:
            return value
            
        #original
        value = translations.get(original)
        if value:
            return value
    
    elif type == "series" or type == "movie":
        for translation in translations:
            if translation.get("language") == language:
                value = translation.get("name")
                if value:
                    return value
        
        # English fallback
        for translation in translations:
            if translation.get("language") == "eng":
                value = translation.get("name")
                if value:
                    return value
        #original     
        for translation in translations:
            if translation.get("language") == original:
                value = translation.get("name")
                if value:
                    return value
                
    elif type in ("series-overview", "movie-overview"):
        for translation in translations:
            if translation.get("language") == language:
                value = translation.get("overview")
                if value:
                    return value
        
        # English fallback
        for translation in translations:
            if translation.get("language") == "eng":
                value = translation.get("overview")
                if value:
                    return value
        #original     
        for translation in translations:
            if translation.get("language") == original:
                value = translation.get("overview")
                if value:
                    return value
                
        for translation in translations:
            if translation.get("language"):
                value = translation.get("overview")
                if value:
                    return value
                
        return _("No synopsis available.")
    
    return None

def get_release_year(response):
    """Return the release year for the media."""
    value = response.get("firstAired") or response.get("first_air_time") or response.get("year")
    if value is None:
        return None

    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None

def get_series_order_type(media_id, order_type=None):
    """Get the matching order type for a TV series."""
    
    cache_key = f"search_order_type_info_{Sources.TVDB.value}_{media_id}"
    data = cache.get(cache_key)
    response = None
    
    if data is not None:
        response = data
    else:
        try:
            response = tvdb.get_series_extended(media_id)
            cache.set(cache_key, data)
        except Exception:
            return "official"

    for season in response.get("seasons", []):
        s_type = season.get("type") or {}
        season_type = s_type.get("type")

        if season_type == order_type:
            return order_type

    return "official"

def search(media_type, query, page, order_type=None):
    """Search for media on TVDB."""
    cache_key = f"search_{Sources.TVDB.value}_{media_type}_{order_type}_{query}_{page}"
    data = cache.get(cache_key)

    if data is not None:
        return data
    
    search_type = "movie"
    if media_type == MediaTypes.TV.value:
        search_type = "series"
    elif media_type == MediaTypes.MOVIE.value:
        search_type =  "movie"
    
    try:
        response = tvdb_api_request(
            "/search",
            {
                "query": query,
                "type": search_type,
                "page": page-1,
            },
        )
    except Exception as error:
        handle_error(error)
        return None
    
    preferred_language = get_tvdb_language()
    
    results = []

    for media in response.get("data", []):
        media_type_tvdb = media.get("type")

        if media_type_tvdb == "series":
            result_type = MediaTypes.TV.value
        elif media_type_tvdb == "movie":
            result_type = MediaTypes.MOVIE.value
        else:
            continue

        translations = media.get("translations", {})
        primary_language = media.get("primary_language")
        preferred_title = None
        original_title = None
        
        if translations:
            preferred_title = get_translation(translations, preferred_language, primary_language, "search")
            original_title = get_translation(translations, primary_language, primary_language, "search")
        
        if not preferred_title:
            preferred_title = media.get("name")
        if not original_title:
            original_title = media.get("name")
        
        media_id = clean_tvdb_id(
            media.get("tvdb_id") or media.get("id")
        )

        results.append(
            {
                "media_id": media_id,
                "source": Sources.TVDB.value,
                "media_type": result_type,
                "title": preferred_title,
                "original_title": original_title,
                "order_type": None,
                "original_language": native_language_name(primary_language),
                "image": get_image_url(media.get("image_url", settings.IMG_NONE)),
                "release_year": get_release_year(media)
            }
        )
        
    if media_type == MediaTypes.TV.value and results:
        
        def enrich_result(result):
            result["order_type"] = get_series_order_type(
                result["media_id"],
                order_type,
            )
            return result

        with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(results)+1)) as executor:
            futures = {
                executor.submit(enrich_result, result): result
                for result in results
            }

            enriched_results = []
            for future in as_completed(futures):
                enriched_results.append(future.result())

        # Preserve the original search order.
        order_map = {
            result["media_id"]: index
            for index, result in enumerate(results)
        }
        results = sorted(
            enriched_results,
            key=lambda r: order_map[r["media_id"]],
        )
        
    total_results = response.get("links", {}).get(
        "total_items",
        len(results),
    )

    per_page = response.get("links", {}).get(
        "page_size",
        50,
    )
    
    data = helpers.format_search_response(
        page,
        per_page,
        total_results,
        results,
    )
    
    cache.set(cache_key, data)

    return data

def find(external_id, external_source):
    """Search for media on TVDB."""
    cache_key = f"find_{Sources.TVDB.value}_{external_id}_{external_source}"
    data = cache.get(cache_key)
    
    

    if data is None:
        try:
            response = tvdb.search_by_remote_id(external_id)
        except Exception as error:
            handle_error(error)
            return

        cache.set(cache_key, response)
        return response

    return data

def translated_status(status):
    """Return a translated TVDB status string."""
    translations = {
        # Movies
        "Rumored": _("Rumored"),
        "Upcoming": _("Planned"),
        "Planned": _("Planned"),
        "Announced": _("Planned"),
        "Pre-Production": _("In Production"),
        "Filming / Post-Production": _("In Production"),
        "In Production": _("In Production"),
        "Post Production": _("Post Production"),
        "Released": _("Released"),
        "Canceled": _("Canceled"),

        # TV Series
        "Returning Series": _("Returning Series"),
        "Continuing": _("Returning Series"),
        "Completed": _("Ended"),
        "Ended": _("Ended"),
        "Pilot": _("Pilot"),
    }

    return translations.get(status, status)

def translated_genre(genre):
    """Return a translated TVDB genre name."""

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
        
        # Missing TVDB genres
        "Mini-Series": _("Mini-Series"),
        "Home and Garden": _("Home and Garden"),
        "Game Show": _("Game Show"),
        "Food": _("Food"),
        "Children": _("Children"),
        "Sport": _("Sport"),
        "Suspense": _("Suspense"),
        "Talk Show": _("Talk Show"),
        "Travel": _("Travel"),
        "Anime": _("Anime"),
        "Musical": _("Musical"),
        "Podcast": _("Podcast"),
        "Indie": _("Indie"),
        "Martial Arts": _("Martial Arts"),
        "Awards Show": _("Awards Show")
    }

    return translations.get(genre, genre)

def get_releases_min_date(releases):
    
    if releases:
        dates = [
            r["date"]
            for r in releases
            if r.get("date")
        ]

        min_date = None
        if dates:
            min_date = min(dates)

    return min_date if min_date else None     

def get_first_none_official_list(lists, preferred_language, primary_language):
    """Return the first official list name."""
    #tvdb.get_list(id) to get the list lang codes
    #we need to grab the porper name and return the primary and original like an araay so we can do get_first_official_list(..)[0] and get_first_official_list(..)[1] to get the proper name and the original name for the list
    
    if not lists:
        return None

    official_list = None
    
    for item in lists:
        if item.get("isOfficial") == False:
            official_list = item
            break
        
    if not official_list:
        return None
    
    cache_key = f"{Sources.TVDB.value}_list_{official_list.get("id")}"
    data = cache.get(cache_key)
    
    if data is not None:
        return data
    
    try:
        list_response = tvdb.get_list_extended(official_list.get("id"))
    except requests.exceptions.HTTPError as error:
        handle_error(error)

    available_languages = official_list.get("nameTranslations", [])    
    
    list_preferred_name = None
    list_original_name = None
    
    if available_languages:
        # Preferred language
        if preferred_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    preferred_language,
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # Original language
        if primary_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    primary_language,
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
        
        # English fallback for preferred
        if not list_preferred_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # English fallback for original
        if not list_original_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
    

    if not list_preferred_name:
        list_preferred_name = official_list.get("name")
    if not list_original_name:
        list_original_name = official_list.get("name")
    
    entities = list_response[0].get("entities", [])
    
    def fetch_entity(entity):
        media_id = None
        media_type = None
        response = None
        preferred_title = None
        original_title = None
        series_id = entity.get("seriesId")
        movie_id = entity.get("movieId")
        if series_id is not None:
            media_id = entity["seriesId"]
            media_type = MediaTypes.TV.value

            try:
                response = tvdb.get_series_extended(media_id, meta="translations", short="false")
            except Exception:
                return None
            
                
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation( name_translations, preferred_language, response.get("originalLanguage"), "series")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "series")
            order_type = "official"
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")

            year= get_release_year(response)
            
        elif movie_id is not None:
            media_id = entity["movieId"]
            media_type = MediaTypes.MOVIE.value
            
            try:
                response = tvdb.get_movie_extended(media_id, meta="translations", short=False)
            except Exception:
                return None
            
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation(name_translations, preferred_language, response.get("originalLanguage"), "movie")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "movie")
            order_type = None
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")
            
            releases = response.get("releases", [])
            min_date = get_releases_min_date(releases)
            year = min_date[:4] if min_date else None   
            if not year:
                year= get_release_year(response)
            
        else:
            return None

        return  {
                "source": Sources.TVDB.value,
                "media_type": media_type,
                "image": get_image_url(response.get("image", settings.IMG_NONE)),
                "media_id": media_id,
                "title": preferred_title,
                "original_title": original_title,
                "order_type": order_type,
                "original_language": native_language_name(response.get("originalLanguage")),
                "release_year": year
            }
        
    
    final = []
    
    with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(entities)+1)) as executor:
        futures = [
            executor.submit(fetch_entity, entity)
            for entity in entities
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
            except requests.exceptions.HTTPError:
                continue

            if result is not None:
                final.append(result)
    
    
    final_list_data = [
        list_preferred_name,
        list_original_name ,
        final
    ]
    
    cache.set(cache_key, final_list_data)

    return final_list_data

def get_first_official_list(lists, preferred_language, primary_language):
    """Return the first official list name."""
    #tvdb.get_list(id) to get the list lang codes
    #we need to grab the porper name and return the primary and original like an araay so we can do get_first_official_list(..)[0] and get_first_official_list(..)[1] to get the proper name and the original name for the list
    if not lists:
        return None

    official_list = None
    
    for item in lists:
        if item.get("isOfficial") == True:
            official_list = item
            break
        
    if not official_list:
        return None
    
    cache_key = f"{Sources.TVDB.value}_list_{official_list.get("id")}"
    data = cache.get(cache_key)
    
    if data is not None:
        return data
    
    try:
        list_response = tvdb.get_list_extended(official_list.get("id"))
    except requests.exceptions.HTTPError as error:
        handle_error(error)
        
    available_languages = official_list.get("nameTranslations", [])    
    
    list_preferred_name = None
    list_original_name = None
    
    if available_languages:
        # Preferred language
        if preferred_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    preferred_language,
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # Original language
        if primary_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    primary_language,
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
        
        # English fallback for preferred
        if not list_preferred_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # English fallback for original
        if not list_original_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
    

    if not list_preferred_name:
        list_preferred_name = official_list.get("name")
    if not list_original_name:
        list_original_name = official_list.get("name")
    
    entities = list_response[0].get("entities", [])
    
    def fetch_entity(entity):
        media_id = None
        media_type = None
        response = None
        preferred_title = None
        original_title = None
        series_id = entity.get("seriesId")
        movie_id = entity.get("movieId")
        if series_id is not None:
            media_id = entity["seriesId"]
            media_type = MediaTypes.TV.value


            response = tvdb.get_series_extended(media_id, meta="translations", short="false")
            
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation( name_translations, preferred_language, response.get("originalLanguage"), "series")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "series")
            order_type = "official"
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")

            year= get_release_year(response)
            
        elif movie_id is not None:
            media_id = entity["movieId"]
            media_type = MediaTypes.MOVIE.value

            try:
                response = tvdb.get_movie_extended(media_id, meta="translations", short=False)
            except Exception:
                return None
                
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation(name_translations, preferred_language, response.get("originalLanguage"), "movie")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "movie")
            order_type = None
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")
            
            releases = response.get("releases", [])
            min_date = get_releases_min_date(releases)
            year = min_date[:4] if min_date else None   
                 
            if not year:
                year= get_release_year(response)
            
        else:
            return None

        return {
                "source": Sources.TVDB.value,
                "media_type": media_type,
                "image": get_image_url(response.get("image", settings.IMG_NONE)),
                "media_id": media_id,
                "title": preferred_title,
                "original_title": original_title,
                "order_type": order_type,
                "original_language": native_language_name(response.get("originalLanguage")),
                "release_year": year
        }

    final = []
    
    with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(entities)+1)) as executor:
        futures = [
            executor.submit(fetch_entity, entity)
            for entity in entities
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
            except requests.exceptions.HTTPError:
                continue

            if result is not None:
                final.append(result)
    
    final_list_data = [
            list_preferred_name,
            list_original_name ,
            final
        ]
        
    cache.set(cache_key, final_list_data)
    # Final fallback
    return final_list_data

def get_first_list(lists, preferred_language, primary_language):
    """Return the first official list name."""
    #tvdb.get_list(id) to get the list lang codes
    #we need to grab the porper name and return the primary and original like an araay so we can do get_first_official_list(..)[0] and get_first_official_list(..)[1] to get the proper name and the original name for the list
    if not lists:
        return None

    official_list = None
    
    for item in lists:
        if item:
            official_list = item
            break
        
    if not official_list:
        return None
    
    cache_key = f"{Sources.TVDB.value}_list_{official_list.get("id")}"
    data = cache.get(cache_key)
    
    if data is not None:
        return data
    
    try:
        list_response = tvdb.get_list_extended(official_list.get("id"))
    except requests.exceptions.HTTPError as error:
        handle_error(error)
        
    available_languages = official_list.get("nameTranslations", [])    
    
    list_preferred_name = None
    list_original_name = None
    
    if available_languages:
        # Preferred language
        if preferred_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    preferred_language,
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # Original language
        if primary_language in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    primary_language,
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
        
        # English fallback for preferred
        if not list_preferred_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_preferred_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass

        # English fallback for original
        if not list_original_name and "eng" in available_languages:
            try:
                translation = tvdb.get_list_translation(
                    official_list.get("id"),
                    "eng",
                )
                list_original_name = translation[0].get("name")
            except requests.exceptions.HTTPError:
                pass
    

    if not list_preferred_name:
        list_preferred_name = official_list.get("name")
    if not list_original_name:
        list_original_name = official_list.get("name")
    
    entities = list_response[0].get("entities", [])
    
    def fetch_entity(entity):
        media_id = None
        media_type = None
        response = None
        preferred_title = None
        original_title = None
        series_id = entity.get("seriesId")
        movie_id = entity.get("movieId")
        if series_id is not None:
            media_id = entity["seriesId"]
            media_type = MediaTypes.TV.value

            try:
                response = tvdb.get_series_extended(media_id, meta="translations", short="false")
            except Exception:
                return None
            
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation( name_translations, preferred_language, response.get("originalLanguage"), "series")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "series")
            order_type = "official"
            
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")

            year= get_release_year(response)
            
        elif movie_id is not None:
            media_id = entity["movieId"]
            media_type = MediaTypes.MOVIE.value

            try:
                response = tvdb.get_movie_extended(media_id, meta="translations", short=False)
            except Exception:
                return None
            
            translations = response.get("translations", {})
            name_translations = translations.get("nameTranslations", [],)
            preferred_title = get_translation(name_translations, preferred_language, response.get("originalLanguage"), "movie")
            original_title = get_translation( name_translations, response.get("originalLanguage"), response.get("originalLanguage"), "movie")
            order_type = None
            if not preferred_title:
                preferred_title = response.get("name")
            if not original_title:
                original_title = response.get("name")
            
            releases = response.get("releases", [])
            min_date = get_releases_min_date(releases)
            year = min_date[:4] if min_date else None   
                 
            if not year:
                year= get_release_year(response)
            
        else:
            return None

        return {
                "source": Sources.TVDB.value,
                "media_type": media_type,
                "image": get_image_url(response.get("image", settings.IMG_NONE)),
                "media_id": media_id,
                "title": preferred_title,
                "original_title": original_title,
                "order_type": order_type,
                "original_language": native_language_name(response.get("originalLanguage")),
                "release_year": year
            }

    final = []
    
    with ThreadPoolExecutor(max_workers=min(max_workers_lists, len(entities)+1)) as executor:
        futures = [
            executor.submit(fetch_entity, entity)
            for entity in entities
        ]

        for future in as_completed(futures):
            try:
                result = future.result()
            except requests.exceptions.HTTPError:
                continue

            if result is not None:
                final.append(result)
    
    final_list_data = [
            list_preferred_name,
            list_original_name ,
            final
        ]
        
    cache.set(cache_key, final_list_data)
    # Final fallback
    return final_list_data

def get_genres(genres):
    """Return the genres for the media."""
    if not genres:
        return None

    return [translated_genre(genre["name"]) for genre in genres if "name" in genre]

def get_score(score):
    """Return the score for the media with one decimal place."""
    # when unknown score, value from response is 0.0
    return round(score, 1)

def get_companies(companies):
    """Return the production companies for the media."""
    if not companies:
        return None

    names = []
    seen = set()

    if isinstance(companies, dict):
        iterable = (
            company
            for company_list in companies.values()
            if company_list
            for company in company_list
        )
    elif isinstance(companies, list):
        iterable = companies
    else:
        return None

    for company in iterable:
        name = company.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    return names or None

def get_country(countries):
    """Return the production countries for the media."""
    if not countries:
        return None

    names = [
        country.get("name")
        for country in countries
        if country.get("name")
    ]

    return ", ".join(names) if names else None

def get_languages(languages):
    """Return the spoken languages for the media."""
    if not languages:
        return None

    names = []

    for code in languages:
        language = native_language_name(code)

        if language:
            names.append(language)

    return names or None

def get_readable_duration(duration):
    """Convert duration in minutes to a readable format."""
    # if unknown movie runtime, value from response is 0
    # e.g movie: 274613
    if duration:
        hours, minutes = divmod(int(duration), 60)
        return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    return None

def movie(media_id, provider):
    """Return the metadata for the selected movie from The Movie Database."""
    cache_key = f"{Sources.TVDB.value}_{provider}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        
        try:
            response = tvdb.get_movie_extended(media_id, meta="translations", short="false")
        except Exception as error:
            handle_error(error)

        preferred_language = get_tvdb_language()
        primary_language = response.get("originalLanguage")
        
        collection_info = get_first_official_list(response.get("lists"), preferred_language, primary_language)
        collection_none_info = get_first_none_official_list(response.get("lists")[:10], preferred_language, primary_language)
        collection_info_name = collection_info[0] if collection_info else None
        
        related = {}
        if collection_info:
            related[collection_info_name] = collection_info[2]

        if collection_none_info:
            related["recommendations"] = collection_none_info[2]
        
        cast = response.get("characters", [])
        cast = [] if cast is None else cast
        filtered_cast = [
            {
                "id": member.get("id"),
                "name": member.get("personName"),
                "url": member.get("url"),
                "character": member.get("name"),
                "image": get_image_url(member.get("image", settings.IMG_NONE)),
            }
            for member in cast[:30]
        ]
        
        translations = response.get("translations", {})
        name_translations = translations.get("nameTranslations", [],)
        overviews = translations.get("overviewTranslations", [])
        preferred_title = get_translation( name_translations, preferred_language, primary_language, "movie")
        original_title = get_translation( name_translations, primary_language, primary_language, "movie")
        overview = get_translation( overviews, preferred_language, primary_language, "movie-overview")
        
        
        if not preferred_title:
            preferred_title = response.get("name")
        if not original_title:
            original_title = response.get("name")
        
        release_date = get_releases_min_date(response.get("releases"))
        release_year = release_date[:4] if release_date else None   
        status = response.get("status")
        status_name = status.get("name") if status else None
        
        
        tmdb_id = next(
            (
                remote["id"]
                for remote in response.get("remoteIds", [])
                if remote.get("sourceName") == "TheMovieDB.com"
            ),
            None,
        )
        
        providers = {}
        if provider == "tmdb":
            if tmdb_id:
                try:
                    providers = tmdb.get_watch_providers(MediaTypes.TV.MOVIE, tmdb_id, provider)
                except Exception:
                    providers = {}
        if providers is None:
            providers = {}
        
        data = {
            "media_id": media_id,
            "source": Sources.TVDB.value,
            "source_url": f"https://www.thetvdb.com/movies/{response.get("slug")}",
            "media_type": MediaTypes.MOVIE.value,
            "title": preferred_title,
            "original_title": original_title,
            "original_language": native_language_name(primary_language),
            "max_progress": 1,
            "order_type": None,
            "image": get_image_url(response.get("image", settings.IMG_NONE)),
            "synopsis": overview,
            "genres": get_genres(response.get("genres")),
            "score": get_score(response.get("score")),
            "score_count": 0,  # TVDB does not provide a score count for movies
            "release_year": release_year,
            "tmdb_id": tmdb_id,
            "details": {
                "format": _("Movie"),
                "release_date": release_date,
                "status": translated_status(status_name),
                "runtime": get_readable_duration(response.get("runtime")),
                "studios": get_companies(response.get("companies")),
                "country": get_country(response.get("production_countries")),
                "languages": get_languages(response.get("spoken_languages")),
            },
            "cast": filtered_cast,
            "total_cast_count": len(cast),
            "related": related,
            "external_links": get_external_links(response.get("remoteIds"), MediaTypes.MOVIE.value),
            "providers": providers,
        }
        
        cache.set(cache_key, data)

    return data

def get_cached_seasons(media_id, season_numbers, order_type=None, provider = None):
    """Check cache for seasons and return cached data and list of uncached seasons."""
    cached_data = {}
    uncached_seasons = []

    for season_number in sorted(season_numbers, key=int):
        season_cache_key = (
            f"{Sources.TVDB.value}_{provider}_{MediaTypes.SEASON.value}_{media_id}_{order_type}_{season_number}"
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
        f"{tv_data["source_url"]}/seasons/{season_number}"
    )
    season_data["title"] = tv_data["title"]
    season_data["original_title"] = tv_data["original_title"]
    season_data["original_language"] = tv_data["original_language"]
    season_data["tmdb_id"] = tv_data["tmdb_id"]
    season_data["external_links"] = tv_data["external_links"]
    season_data["genres"] = tv_data["genres"]
    season_data["release_year"] = tv_data["release_year"]
    if season_data["synopsis"] == _("No synopsis available."):
        season_data["synopsis"] = tv_data["synopsis"]
    return season_data

def fetch_and_cache_seasons(media_id, season_numbers, tv_data, order_type=None, provider = None):
    """Fetch uncached seasons from API and cache them."""

    fetched_tv_data = tv_data

    try:
        response = tvdb.get_series_extended(media_id, meta="translations", short="false")
    except Exception as error:
        handle_error(error)

    # Cache TV metadata if we haven't fetched it yet
    if fetched_tv_data is None:
        fetched_tv_data = process_tv(response, media_id, order_type, provider)
        tv_cache_key = f"{Sources.TVDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}_{order_type}"
        cache.set(tv_cache_key, fetched_tv_data)

    result_data = {}

    season_numbers_int = {int(n) for n in season_numbers}
    seasons_to_fetch = []
    preferred_seasons = []
    official_seasons = []


    for season in response.get("seasons", []):

        s_type = season.get("type")

        if not s_type:
            continue
        
        season_number = season.get("number")
        if season_number is None or int(season_number) not in season_numbers_int:
            continue
        
        season_type = s_type.get("type")

        if season_type == order_type:
            preferred_seasons.append((season, season_number))
        elif season_type == "official":
            official_seasons.append((season, season_number))
        
    # Use preferred seasons if any exist, otherwise fall back to official.
    seasons_to_fetch = preferred_seasons or official_seasons

    def fetch_season(season, season_number):
        
        try:
            season_response = tvdb.get_season_extended(
                season.get("id"),
                meta="translations",
            )
        except Exception:
            return None

        season_response["tmdb_id"] = fetched_tv_data["tmdb_id"]
        season_data = process_season(
            season_response,
            provider,
            response.get("originalLanguage"),
            media_id,
            order_type,
        )
        
        if season_data is None:
            return None

        season_data = enrich_season_with_tv_data(
            season_data,
            fetched_tv_data,
            media_id,
            season_number,
        )
        
        if season_data is None:
            return None

        cache.set(
            f"{Sources.TVDB.value}_{provider}_{MediaTypes.SEASON.value}_{media_id}_{order_type}_{season_number}",
            season_data,
        )

        return season_number, season_data

    errors = []
    season_results = []

    with ThreadPoolExecutor(max_workers=min(max_workers_seasons, len(seasons_to_fetch)+1)) as executor:
        futures = {
            executor.submit(fetch_season, season, season_number): season_number
            for season, season_number in seasons_to_fetch
        }

        for future in as_completed(futures):
            season_number = futures[future]
            result = future.result()
            if result is None:
                continue
            
            try:
                season_results.append(result)
            except Exception as error:
                errors.append((season_number, error))

    
    for season_number, season_data in sorted(season_results, key=lambda x: x[0]):
        result_data[f"season/{season_number}"] = season_data
    
    if errors:
        season_number, error = errors[0]

        msg = _(
            "Season %(season_number)s not found in %(source)s with ID %(media_id)s"
        ) % {
            "season_number": season_number,
            "source": Sources.TVDB.label,
            "media_id": media_id,
        }

        not_found_response = requests.Response()
        not_found_response.status_code = 404
        not_found_error = type("Error", (), {"response": not_found_response})

        raise services.ProviderAPIError(
            msg,
            error=not_found_error,
            details=msg,
        )

    return result_data, fetched_tv_data

def tv_with_seasons(media_id, season_numbers, order_type=None, provider = None):
    """Return the metadata for the tv show with seasons appended to the response."""
    if not season_numbers:
        return tv(media_id, order_type)

    tv_cache_key = f"{Sources.TVDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}_{order_type}"
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
            provider
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
    cache_key = f"{Sources.TVDB.value}_{provider}_{MediaTypes.TV.value}_{media_id}_{order_type}"
    data = cache.get(cache_key)

    if data is None:
        try:
            response = tvdb.get_series_extended(media_id, meta="translations", short="false")
        except Exception as error:
            handle_error(error)

        data = process_tv(response, media_id, order_type, provider)
        cache.set(cache_key, data)

    return data

def get_start_date(date):
    """Return the start date for the media."""
    # when unknown date, value from response is empty string
    # e.g movie: 445290
    if date == "":
        return None
    return date

def get_related(related_medias):
    """Return list of related media for the selected media."""
    related = []
    
    for media in related_medias:
        
        dates = [
            episode["aired"]
            for episode in media["episodes"]
            if episode.get("aired")
        ]
        
        first_air_date = min(dates) if dates else None
        year = first_air_date[:4] if first_air_date else None
        
        data = {
            "source": Sources.TVDB.value,
            "media_type": media.get("media_type"),
            "image": get_image_url(media.get("image", settings.IMG_NONE)),
            "year": media.get("year", year),
            "media_id": media.get("parent_id"),
            "title": media.get("parent_title"),
            "original_title": media.get("parent_original_title"),
            "season_number": media.get("number"),
            "season_title": media.get("season_title"),
            "order_type": media.get("type", {}).get("type", "official"),
            "season_original_title": media.get("season_original_title"),
            "first_air_date": first_air_date,
            "season_release_year": first_air_date[:4] if first_air_date else None,
            "max_progress": len(media.get("episodes", []))
        }
        related.append(data)
    
    related.sort(key=lambda x: int(x["season_number"]))
    
    return related

def process_tv(response, media_id, order_type=None, provider = None):
    """Process the metadata for the selected tv show from The Movie Database."""
    
    preferred_language = get_tvdb_language()
    primary_language = response.get("originalLanguage")
    
    collection_info = get_first_list(response.get("lists")[:10], preferred_language, primary_language)
    collection_info_name = collection_info[0] if collection_info else None
    
    cast = response.get("characters", [])
    cast = [] if cast is None else cast
    filtered_cast = [
        {
            "id": member.get("id"),
            "name": member.get("personName"),
            "character": member.get("name"),
            "image": get_image_url(member.get("image", settings.IMG_NONE)),
        }
        for member in cast[:30]
    ]
    
    translations = response.get("translations", {})
    name_translations = translations.get("nameTranslations", [],)
    overviews = translations.get("overviewTranslations", [])
    preferred_title = get_translation( name_translations, preferred_language, primary_language, "series")
    original_title = get_translation( name_translations, primary_language, primary_language, "series")
    overview = get_translation( overviews, preferred_language, primary_language, "series-overview")
    
    
    if not preferred_title:
        preferred_title = response.get("name")
    if not original_title:
        original_title = response.get("name")
    if overview == _("No synopsis available."):
        original_overview = response.get("overview")
        if original_overview is not None and original_overview != "":
            overview = original_overview
    
    
    status = response.get("status")
    status_name = status.get("name") if status else None
    country_id = response.get("originalCountry")
    country_alpha = pycountry.countries.get(alpha_3=country_id.upper())
    country = country_alpha.name if country_alpha else None 
    language = native_language_name(primary_language)
    
    today = date.today().isoformat()
    official_seasons = []
    episodes = []
    seasons = []
    preferred_seasons = []
    falback_seasons = []
    
    for season in response.get("seasons", []):
        s_type = season.get("type")
        if not s_type:
            continue

        season_id = season.get("id")
        if not season_id:
            continue

        season_type = s_type.get("type")

        if season_type == order_type:
            preferred_seasons.append(season_id)
        elif season_type == "official":
            falback_seasons.append(season_id)
            
    # Use preferred seasons if any exist, otherwise fall back to official.
    official_seasons = preferred_seasons or falback_seasons
    
    def fetch_season(season_id):
        
        try:
            season_response = tvdb.get_season_extended(season_id, meta="translations")
        except Exception:
            return None
            
        episodes.extend(
            season_response.get("episodes", [])
        )
        
        season_response["media_type"] = MediaTypes.SEASON.value
        season_base_translations = season_response.get("translations", {})
        season_name_translations  = season_base_translations.get("nameTranslations", [])
        season_preferred_title = get_translation( season_name_translations, preferred_language, primary_language, "series")
        season_original_title = get_translation( season_name_translations, primary_language, primary_language, "series")
    
        if not season_preferred_title:
            season_preferred_title = response.get("name")
        if not season_original_title:
            season_original_title = response.get("name")
        
        season_response["season_title"] = season_preferred_title
        season_response["season_original_title"] = season_original_title
        season_response["parent_id"] = media_id
        season_response["parent_title"] = preferred_title
        season_response["parent_original_title"] = original_title
        
        return season_response

    with ThreadPoolExecutor(max_workers=min(max_workers_seasons, len(official_seasons)+1)) as executor:
        futures = [
            executor.submit(fetch_season, season_id)
            for season_id in official_seasons
        ]

        for future in as_completed(futures):
            try:
                season_response = future.result()
            except Exception:
                continue
            
            if season_response is None:
                continue
                
            episodes.extend(season_response.get("episodes", []))
            seasons.append(season_response)
    
    seasons.sort(key=lambda season: int(season.get("number", 0)))
    valid_episodes = []
    last_aired_episode_season = None
    next_episode_season = None
    if episodes:
        
        episodes.sort(
            key=lambda ep: (
                int(ep.get("seasonNumber", 0)),
                int(ep.get("number", 0)),
            )
        )
        
        # Remove episodes without a season/episode number
        valid_episodes = [
            episode
            for episode in episodes
            if episode.get("seasonNumber") is not None
            and episode.get("number") is not None
        ]

        aired_episodes = [
            episode
            for episode in valid_episodes
            if episode.get("aired")
            and episode.get("aired") <= today
        ]

        upcoming_episodes = [
            episode
            for episode in valid_episodes
            if episode.get("aired")
            and episode.get("aired") > today
        ]

        if aired_episodes:
            last_episode = max(
                aired_episodes,
                key=lambda x: x.get("aired"),
            )
            last_aired_episode_season = last_episode.get(
                "seasonNumber"
            )

        if upcoming_episodes:
            next_episode = min(
                upcoming_episodes,
                key=lambda x: x.get("aired"),
            )
            next_episode_season = next_episode.get(
                "seasonNumber"
            )
            
    related = {}
    if seasons:
        related[_("seasons")] = get_related(seasons)
    if collection_info:
        related[_("recommendations")] = collection_info[2]
    
    tmdb_id = next(
        (
            remote["id"]
            for remote in response.get("remoteIds", [])
            if remote.get("sourceName") == "TheMovieDB.com"
        ),
        None,
    )
    
    providers = {}
    if provider == "tmdb":
        if tmdb_id:
            try:
                providers = tmdb.get_watch_providers(MediaTypes.TV.value, tmdb_id, provider)
            except Exception:
                providers = {}
    if providers is None:
        providers = {}
    
    
    next_air = get_start_date(response.get("nextAired", None))
    if next_air is None:
        next_air = 'N/A'
    
    data = {
        "media_id": media_id,
        "source": Sources.TVDB.value,
        "source_url": f"https://www.thetvdb.com/series/{response.get("slug")}",
        "media_type": MediaTypes.TV.value,
        "title": preferred_title,
        "original_title": original_title,
        "original_language": language,
        "release_year": get_release_year(response),
        "max_progress": len(valid_episodes),
        "order_type": order_type,
        "image": get_image_url(response.get("image", settings.IMG_NONE)),
        "synopsis": overview,
        "genres": get_genres(response.get("genres")),
        "score": get_score(response.get("score")),
        "score_count": 0,  # TVDB does not provide a score count for movies
        "details": {
            "format": _("TV"),
            "first_air_date": get_start_date(response.get("firstAired", None)),
			"last_air_date": get_start_date(response.get("lastAired", None)),
            "next_air_date": next_air,
            "status": translated_status(status_name),
			"seasons": len(seasons),
			"episodes": len(valid_episodes),
            "runtime": get_readable_duration(response.get("averageRuntime")),
            "studios": get_companies(response.get("companies")),
            "country": country,
            "languages": language,
        },
        "cast": filtered_cast,
        "total_cast_count": len(cast),
        "related": related,
        "external_links": get_external_links(response.get("remoteIds"), MediaTypes.TV.value),
		"tmdb_id": tmdb_id,
		"last_episode_season": last_aired_episode_season,
        "next_episode_season": next_episode_season,
        "providers": providers,
    }
    
    return data

def process_episode(episode, preferred_language, primary_language, order_type=None):
    """Fetch and process a single episode."""
    
    try:
        episode_response = tvdb.get_episode_extended(episode["id"], meta="translations")
    except Exception:
        return None
    print(episode["id"])

    episode_base_translations = episode_response.get("translations", {})
    episode_name_translations = episode_base_translations.get("nameTranslations", [])
    episode_overviews = episode_base_translations.get("overviewTranslations", [])

    episode_preferred_title = get_translation(episode_name_translations, preferred_language, primary_language, "series")
    episode_original_title = get_translation( episode_name_translations, primary_language, primary_language, "series")

    episode_preferred_overview = get_translation(episode_overviews, preferred_language, primary_language, "series-overview")
    
    if not episode_preferred_title:
        episode_preferred_title = episode_response.get("name")
    if not episode_original_title:
        episode_original_title = episode_response.get("name")
    if episode_preferred_overview == _("No synopsis available."):
        original_overview = episode_response.get("overview")
        if original_overview:
            episode_preferred_overview = original_overview


    cast = episode_response.get("characters", [])
    cast = [] if cast is None else cast

    filtered_cast = [
        {
            "department": _("Crew"),
            "job": member.get("peopleType"),
            "credit_id": None,
            "adult": None,
            "gender": None,
            "id": member.get("id"),
            "known_for_department": None,
            "name": member.get("personName"),
            "original_name": member.get("personName"),
            "popularity": None,
            "profile_path": get_image_url(member.get("image", settings.IMG_NONE)),
        }
        for member in cast
    ]
   
    data =  {
        "runtime": episode_response.get("runtime"),
        "episode_data": {
            "air_date": episode_response.get("aired"),
            "episode_number": episode_response.get("number"),
            "episode_type": episode_response.get("finaleType"),
            "id": episode_response.get("id"),
            "name": episode_preferred_title,
            "original_name": episode_original_title,
            "overview": episode_preferred_overview,
            "order_type": order_type,
            "production_code": episode_response.get("productionCode"),
            "runtime": episode_response.get("runtime"),
            "season_number": episode_response.get("seasonNumber"),
            "show_id": episode_response.get("seriesId"),
            "still_path": get_image_url(episode_response.get("image", settings.IMG_NONE)),
            "vote_average": 0,
            "vote_count": 0,
            "crew": filtered_cast,
        }
    }

    return data
    
def process_season(response, provider, primary_language, media_id, order_type=None):
    """Process the metadata for the selected season from The Movie Database."""
    season_response = response
    episodes = season_response.get("episodes", [])
    
    num_episodes = len(episodes)

    if len(episodes) == 0:
        return None

    preferred_language = get_tvdb_language()
    runtimes = []
    total_runtime = 0
    season_episodes = [None] * len(episodes)
    
    with ThreadPoolExecutor(max_workers=min(max_workers_episodes, len(episodes)+1)) as executor:
        futures = {
            executor.submit(
                process_episode,
                episode,
                preferred_language,
                primary_language,
                order_type,
            ): index
            for index, episode in enumerate(episodes)
        }

        for future in as_completed(futures):
            index = futures[future]

            try:
                result = future.result()
            except Exception:
                continue

            if result is None:
                continue

            runtime = result["runtime"]
            if runtime is not None:
                runtimes.append(runtime)
                total_runtime += runtime

            season_episodes[index] = result["episode_data"]
    
    # Remove failed episodes
    print(season_episodes)
    season_episodes = [ep for ep in season_episodes if ep is not None]
    season_episodes.sort(key=lambda ep: int(ep.get("episode_number", 0)))

    avg_runtime = (
        get_readable_duration(sum(runtimes) / len(runtimes)) if runtimes else None
    )
    total_runtime = get_readable_duration(total_runtime) if total_runtime else None
    
    season_base_translations = season_response.get("translations", {})
    season_name_translations  = season_base_translations.get("nameTranslations", [])
    season_overviews = season_base_translations.get("overviewTranslations", [])
    
    season_preferred_title = get_translation(season_name_translations, preferred_language, primary_language, "series")
    season_original_title = get_translation(season_name_translations, primary_language, primary_language, "series")
    season_preferred_overview = get_translation(season_overviews, preferred_language, primary_language, "series-overview")
    
    if not season_preferred_title:
        season_preferred_title = season_response.get("name")
    if not season_original_title:
        season_original_title = season_response.get("name")
    if season_preferred_overview == _("No synopsis available."):
        original_overview = season_response.get("overview")
        if original_overview is not None and original_overview != "":
            season_preferred_overview = original_overview
    
    today = date.today().isoformat()
    
    first_air_dates = [
        episode["air_date"]
        for episode in season_episodes
        if episode.get("air_date")
    ]
    
    first_air_date = min(first_air_dates) if first_air_dates else None
    
    aired_dates = [
        air_date
        for air_date in first_air_dates
        if air_date <= today
    ]
    
    tmdb_id = season_response.get("tmdb_id", None)
    providers = {}
    if provider == "tmdb":
        if tmdb_id:
            try:
                providers = tmdb.get_watch_providers(MediaTypes.TV.value, tmdb_id, provider)
            except Exception:
                providers = {}
    if providers is None:
        providers = {}
    
    final_data = {
        "source": Sources.TVDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": season_preferred_title,
        "season_original_title": season_original_title,
        "season_release_year": first_air_date[:4] if first_air_date else None,
        "max_progress": season_episodes[-1]["episode_number"] if season_episodes else 0,
        "image": get_image_url(season_response.get("image", settings.IMG_NONE)),
        "season_number": season_response.get("number"),
        "order_type": order_type,
        "synopsis": season_preferred_overview,
        "score": 0,
        "score_count": 0,
        "details": {
            "first_air_date": min(first_air_dates) if first_air_dates else season_response.get("year"),
            "last_air_date": max(aired_dates) if aired_dates else None,
            "episodes": num_episodes,
            "runtime": avg_runtime,
            "total_runtime": total_runtime,
        },
        "episodes": season_episodes,
        "providers": providers,
    }
    
    return final_data
  
def get_format(media_type):
    """Return media_type capitalized."""
    if media_type == MediaTypes.TV.value:
        return _("TV")
    return _("Movie")

def get_runtime_tv(runtime):
    """Return the runtime for the tv show."""
    # when unknown runtime, value from response is empty list
    # e.g: tv:66672
    if runtime:
        return get_readable_duration(runtime[0])
    return None

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
            provider["image"] = get_image_url(tmdb.get_image_url(provider.get("logo_path")))

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
        air_Date = episode.get("air_date")
        episodes_metadata.append(
            {
                "media_id": season_metadata.get("media_id"),
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TVDB.value,
                "season_number": season_metadata.get("season_number"),
                "episode_number": episode_number,
                "air_date": air_Date,
                "order_type": order_type,
                "episode_release_year": int(air_Date[:4]),
                "image": get_image_url(episode.get("still_path", settings.IMG_NONE)),
                "title": episode.get("name"),
                "original_title": episode.get("original_name"),
                "overview": episode.get("overview"),
                "history": tracked_episodes.get(episode_number, []),
                "runtime": get_readable_duration(episode.get("runtime")),
                "runtime_minutes": episode.get("runtime"),
            },
        )
        
    episodes_metadata.sort(key=lambda ep: int(ep["episode_number"]))
    
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
            air_Date = episode.get("air_date")
            return {
                "title": season_metadata.get("title"),
                "original_title": season_metadata.get("original_title"),
                "season_title": season_metadata.get("season_title"),
                "season_original_title": season_metadata.get("season_original_title"),
                "season_release_year": season_metadata.get("season_release_year"),
                "episode_title": episode.get("name"),
                "order_type": order_type,
                "episode_original_title": episode.get("original_name"),
                "air_date": air_Date,
                "episode_release_year": int(air_Date[:4]),
                "image": get_image_url(episode.get("still_path", settings.IMG_NONE)),
            }

    # Episode not found - throw ProviderAPIError
    msg = (
        f"Episode {episode_number} not found in season {season_number} "
        f"for {Sources.TVDB.label} with ID {media_id}"
    )
    # Create a new response object with 404 status
    not_found_response = requests.Response()
    not_found_response.status_code = 404
    # Set the error attribute to match what ProviderAPIError expects
    not_found_error = type("Error", (), {"response": not_found_response})
    raise services.ProviderAPIError(
        Sources.TVDB.value,
        error=not_found_error,
        details=msg,
    )

def watch_provider_regions(provider):
    """Return the available watch provider regions from The Movie Database."""
    if provider == "tmdb":
        cache_key = f"{Sources.TVDB.value}_provider_watch_provider_regions"
        data = cache.get(cache_key)

        if data is None:
            url = f"{tmdb.get_base_url_tmdb()}/watch/providers/regions"
            params = {**tmdb.tmdb_get_base_params_prefered()}

            try:
                response = services.api_request(
                    Sources.TVDB.value,
                    "GET",
                    url,
                    params=params,
                )
            except requests.exceptions.HTTPError as error:
                handle_error(error)

            data = [("", "Disabled")]
            regions = response.get("results", [])
            for region in sorted(regions, key=lambda r: r.get("english_name", "")):
                key = region.get("iso_3166_1")
                name = region.get("english_name")
                if key:
                    if not name:
                        name = key
                    data.append((key, name))

            cache.set(cache_key, data)
            
    return data

def get_changed_ids(media_type):
    """Return changed TVDB ids for the given media type over the last days."""
    start_timestamp = int(
        (timezone.now() - timedelta(days=3)).timestamp()
    )
    changed_ids = set()
    page = 0
    selected_type = None
    
    if media_type == MediaTypes.TV.value:
        selected_type = "series"
    elif media_type == MediaTypes.MOVIE.value:
        selected_type = "movies"
    elif media_type == MediaTypes.SEASON.value:
        selected_type = "seasons"
    elif media_type == MediaTypes.EPISODE.value:
        selected_type = "episodes"
    else:
        return changed_ids
   
    while True:
        
        try:
            response = tvdb_api_request(
                "/updates",
                {
                    "since": start_timestamp,
                    "type": selected_type,
                    "page": page,
                },
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)
            return changed_ids
        
        
        changed_ids.update(str(result["recordId"]) for result in response.get("data", []))
        links = response.get("links")
        total_items = links.get("total_items")
        page_size = links.get("page_size")
        total_pages = total_items / page_size
        if page >= total_pages:
            break
        page += 1

    return changed_ids

def tv_changes():
    """Return changed TV ids from TVDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.TV.value)

def movie_changes():
    """Return changed movie ids from TVDB for the last days across all pages."""
    return get_changed_ids(MediaTypes.MOVIE.value)

def get_image_url(path):
    """Return the image URL for the media."""
    # when no image, value from response is null
    # e.g movie: 445290
    if path is None or path == '':
        return settings.IMG_NONE
    
    return path
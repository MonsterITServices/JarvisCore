import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class BraveSearchError(Exception):
    pass


def is_configured():
    return bool(os.environ.get("BRAVE_SEARCH_API_KEY"))


def search(query, count=5):
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not api_key:
        raise BraveSearchError("Set BRAVE_SEARCH_API_KEY in your user environment to enable web search.")

    parameters = urlencode({"q": query[:400], "count": max(1, min(count, 8))})
    http_request = Request(
        f"https://api.search.brave.com/res/v1/web/search?{parameters}",
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    try:
        with urlopen(http_request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            raise BraveSearchError("Brave rejected the search key or its access plan.") from error
        raise BraveSearchError(f"Brave Search returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise BraveSearchError("Brave Search could not be reached or timed out.") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BraveSearchError("Brave Search returned an invalid response.") from error

    results = []
    for item in payload.get("web", {}).get("results", []):
        title = item.get("title")
        url = item.get("url")
        description = item.get("description")
        if not all(isinstance(value, str) and value for value in (title, url, description)):
            continue
        if not url.startswith("https://"):
            continue
        results.append({"title": title[:300], "url": url[:2000], "description": description[:1500]})
        if len(results) >= count:
            break
    return results
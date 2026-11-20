"""BookHunter: ricerca libri multi-catalogo.

Google Books richiede GOOGLE_BOOKS_API_KEY.
Open Library è usato come fonte di riserva; le risposte vengono memorizzate
nella stessa base SQLite di BookHunter per ridurre le richieste ripetute.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger("bookhunter.catalog")
GOOGLE_URL = "https://www.googleapis.com/books/v1/volumes"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json"
DB_PATH = os.getenv("DATABASE_PATH", "/data/bookhunter.sqlite3")
CACHE_HOURS = 8
PUBLISHERS = [
    "Adelphi", "Einaudi", "Sellerio", "Feltrinelli",
    "Mondadori", "Neri Pozza", "Iperborea", "Fazi"
]


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _cache_key(kind: str, value: str) -> str:
    return kind + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cache_get(key: str, hours: int = CACHE_HOURS):
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=8) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS catalog_cache "
                "(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            row = con.execute(
                "SELECT payload,fetched_at FROM catalog_cache WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row[1])
        if (datetime.now(timezone.utc) - fetched).total_seconds() > hours * 3600:
            return None
        return json.loads(row[0])
    except Exception:
        log.exception("Errore lettura cache catalogo")
        return None


def _cache_set(key: str, value) -> None:
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=8) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS catalog_cache "
                "(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            con.execute(
                "INSERT INTO catalog_cache VALUES(?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload,fetched_at=excluded.fetched_at",
                (key, json.dumps(value, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat())
            )
            con.execute(
                "DELETE FROM catalog_cache WHERE fetched_at < ?",
                ((datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),)
            )
    except Exception:
        log.exception("Errore scrittura cache catalogo")


def _parse_query(raw: str):
    """Legge i formati espliciti e riconosce ISBN; altrimenti lascia la query libera."""
    compact = raw.strip()
    isbn = re.sub(r"[\s-]", "", compact)
    if re.fullmatch(r"(?:\d{9}[\dXx]|\d{13})", isbn):
        return "", "", isbn

    title_match = re.search(
        r"(?:^|\s)(?:titolo|title)\s*:\s*(.+?)(?=\s+(?:autore|author)\s*:|$)",
        compact, re.I
    )
    author_match = re.search(
        r"(?:^|\s)(?:autore|author)\s*:\s*(.+?)(?=\s+(?:titolo|title)\s*:|$)",
        compact, re.I
    )
    title = title_match.group(1).strip().strip('"\'') if title_match else ""
    author = author_match.group(1).strip().strip('"\'') if author_match else ""
    return title, author, ""


def _query_variants(raw: str):
    title, author, isbn = _parse_query(raw)
    if isbn:
        return [f"isbn:{isbn}"]
    if title and author:
        return [f'intitle:"{title}" inauthor:"{author}"']
    if title:
        return [f'intitle:"{title}"', title]
    if author:
        return [f'inauthor:"{author}"', author]

    variants = [raw]
    words = raw.split()
    if len(words) >= 3:
        # Prova le divisioni titolo/autore dal centro; se compare "di" prima
        # del nome, prova anche a rimuoverlo dal campo autore.
        splits = sorted(range(1, len(words)), key=lambda n: abs(n - len(words) / 2))
        connector_splits = [
            n for n in range(1, len(words))
            if words[n].casefold() in ("di", "by", "from", "de")
        ]
        ordered_splits = list(dict.fromkeys(connector_splits + splits))
        for split in ordered_splits[:5]:
            left = " ".join(words[:split]).strip('"')
            right_words = words[split:]
            if right_words and right_words[0].casefold() in ("di", "by", "from", "de"):
                right_words = right_words[1:]
            right = " ".join(right_words).strip('"')
            if left and right:
                variants.append(f'intitle:"{left}" inauthor:"{right}"')
    elif len(words) == 2:
        variants.extend([f'inauthor:"{raw}"', f'intitle:"{raw}"'])
    return list(dict.fromkeys(variants))[:5]


def _google_search(query: str, limit: int, *, newest=False, italian=True):
    api_key = os.getenv("GOOGLE_BOOKS_API_KEY", "").strip()
    if not api_key:
        return []
    params = {
        "q": query, "key": api_key,
        "maxResults": min(max(int(limit), 1), 40),
        "printType": "books", "projection": "full",
        "orderBy": "newest" if newest else "relevance",
    }
    if italian:
        params["langRestrict"] = "it"
    if newest:
        params["showPreorders"] = "true"
    try:
        response = requests.get(GOOGLE_URL, params=params, timeout=(8, 18))
        if response.status_code in (403, 429):
            log.warning("Google Books quota/autorizzazione: HTTP %s", response.status_code)
            return []
        response.raise_for_status()
        return response.json().get("items", []) or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("Google Books non disponibile: %s", exc)
        return []


def _google_book(item: dict[str, Any]):
    info = item.get("volumeInfo") or {}
    title = (info.get("title") or "").strip()
    if not title:
        return None
    ids = info.get("industryIdentifiers") or []
    isbn = next((x.get("identifier", "") for x in ids if x.get("type") == "ISBN_13"), "")
    if not isbn:
        isbn = next((x.get("identifier", "") for x in ids if x.get("type") == "ISBN_10"), "")
    language = (info.get("language") or "").lower()
    images = info.get("imageLinks") or {}
    cover = images.get("thumbnail") or images.get("smallThumbnail") or ""
    if cover.startswith("http://"):
        cover = "https://" + cover[7:]
    description = re.sub(r"<[^>]+>", " ", info.get("description", "") or "")
    return {
        "title": title,
        "authors": ", ".join(info.get("authors") or []),
        "publisher": info.get("publisher") or "",
        "published": info.get("publishedDate") or "",
        "description": description[:1600],
        "categories": ", ".join(info.get("categories") or []),
        "isbn": isbn,
        "link": info.get("infoLink") or item.get("selfLink") or "",
        "source": "Google Books",
        "italian_verified": language == "it",
        "language": language,
        "cover": cover,
    }


def _is_italian(languages):
    if isinstance(languages, str):
        languages = [languages]
    return any(
        str(value).strip().lower().rstrip("/") in ("ita", "/languages/ita")
        or str(value).strip().lower().endswith("/ita")
        for value in (languages or [])
    )


def _openlibrary_search(query: str, limit: int):
    title, author, isbn = _parse_query(query)
    if not title and not author and not isbn:
        variants = _query_variants(query)
        if len(variants) > 1 and variants[1].startswith("intitle:"):
            inferred = re.match(r'intitle:"(.*?)"\\s+inauthor:"(.*?)"', variants[1])
            if inferred:
                title, author = inferred.group(1), inferred.group(2)
    if isbn:
        q = f"isbn:{isbn}"
    elif title and author:
        q = f'title:"{title}" author:"{author}"'
    elif title:
        q = f'title:"{title}"'
    elif author:
        q = f'author:"{author}"'
    else:
        q = query

    fields = ",".join([
        "key", "title", "author_name", "first_publish_year", "publisher",
        "subject", "isbn", "cover_i", "editions.key", "editions.title",
        "editions.language", "editions.publish_date", "editions.publishers",
        "editions.isbn_13", "editions.isbn_10"
    ])
    params = {"q": q + " language:ita", "limit": min(max(limit, 1), 20),
              "fields": fields, "lang": "it"}
    try:
        response = requests.get(
            OPEN_LIBRARY_URL, params=params,
            headers={"User-Agent": "BookHunterTelegramBot/2.0 (personal book search)"},
            timeout=(8, 18)
        )
        if response.status_code == 429:
            log.warning("Open Library ha applicato un limite temporaneo")
            return []
        response.raise_for_status()
        docs = response.json().get("docs", []) or []
        # Se il filtro lingua è troppo restrittivo, recupera le edizioni
        # non filtrate: saranno mostrate come lingua non verificata.
        if not docs:
            fallback_params = dict(params)
            fallback_params["q"] = q
            fallback = requests.get(
                OPEN_LIBRARY_URL, params=fallback_params,
                headers={"User-Agent": "BookHunterTelegramBot/2.0 (personal book search)"},
                timeout=(8, 18)
            )
            fallback.raise_for_status()
            docs = fallback.json().get("docs", []) or []
    except (requests.RequestException, ValueError) as exc:
        log.warning("Open Library non disponibile: %s", exc)
        return []

    results = []
    for doc in docs:
        editions = (doc.get("editions") or {}).get("docs") or []
        italian = [e for e in editions if _is_italian(e.get("language"))]
        edition = italian[0] if italian else (editions[0] if editions else {})
        edition_title = (edition.get("title") or "").strip()
        book_title = edition_title or (doc.get("title") or "").strip()
        if not book_title:
            continue

        def as_list(value):
            if value is None:
                return []
            return value if isinstance(value, list) else [value]

        publishers = as_list(edition.get("publishers") or doc.get("publisher"))
        isbns = as_list(edition.get("isbn_13")) or as_list(edition.get("isbn_10"))
        isbns += as_list(doc.get("isbn"))
        dates = as_list(edition.get("publish_date"))
        key = edition.get("key") or doc.get("key") or ""
        results.append({
            "title": book_title,
            "authors": ", ".join(doc.get("author_name") or []),
            "publisher": ", ".join(str(x) for x in publishers[:3]),
            "published": ", ".join(str(x) for x in dates[:2]) or str(doc.get("first_publish_year") or ""),
            "description": "",
            "categories": ", ".join((doc.get("subject") or [])[:10]),
            "isbn": next(iter(isbns), ""),
            "link": "https://openlibrary.org" + key if key.startswith("/") else key,
            "source": "Open Library",
            "italian_verified": bool(italian),
            "language": "it" if italian else "",
            "cover": (
                f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg"
                if doc.get("cover_i") else ""
            ),
        })
    return results


def _identity(book):
    isbn = re.sub(r"[^0-9Xx]", "", str(book.get("isbn") or ""))
    if isbn:
        return "isbn:" + isbn
    return "title:" + _norm(book.get("title", "")) + "|" + _norm(book.get("authors", ""))



def _merge(items, query, limit):
    merged = {}
    for book in items:
        key = _identity(book)
        if key not in merged:
            merged[key] = dict(book)
            continue
        old = merged[key]
        for field in ("title", "authors", "publisher", "published",
                      "description", "categories", "isbn", "link",
                      "cover", "language"):
            if not old.get(field) and book.get(field):
                old[field] = book[field]
        old["italian_verified"] = bool(
            old.get("italian_verified") or book.get("italian_verified")
        )
        sources = set(str(old.get("source", "")).split(" + "))
        sources.update(str(book.get("source", "")).split(" + "))
        old["source"] = " + ".join(sorted(s for s in sources if s))

    raw = (query or "").strip()
    normalized = _norm(raw)
    title_query = normalized
    author_query = ""

    if "|" in raw:
        parts = raw.split("|", 1)
        title_query = _norm(parts[0])
        author_query = _norm(parts[1])
    elif " di " in normalized:
        title_query, author_query = normalized.rsplit(" di ", 1)

    title_tokens = [
        t for t in title_query.split()
        if len(t) > 2 or t.isdigit()
    ]
    author_tokens = [
        t for t in author_query.split()
        if len(t) > 2
    ]

    def score(book):
        title = _norm(book.get("title", ""))
        author = _norm(book.get("authors", ""))
        points = 0

        if title_query and title == title_query:
            points += 100
        elif title_query and title_query in title:
            points += 60

        if title_tokens:
            matches = sum(t in title.split() for t in title_tokens)
            if matches == len(title_tokens):
                points += 40
            else:
                points += matches * 8
                points -= (len(title_tokens) - matches) * 15

        if author_tokens:
            matches = sum(t in author for t in author_tokens)
            points += matches * 12
            points -= (len(author_tokens) - matches) * 12

        if book.get("italian_verified"):
            points += 5

        return points

    results = list(merged.values())
    results.sort(
        key=lambda b: (
            -score(b),
            not b.get("italian_verified"),
            _norm(b.get("title", ""))
        )
    )
    return results[:min(max(int(limit), 1), 20)]



def search_books(query: str, limit: int = 8):
    """Cerca su Google Books e Open Library, privilegiando le edizioni italiane."""
    raw = (query or "").strip()
    if not raw:
        return []
    cache_key = _cache_key("search", raw.casefold() + "|" + str(limit))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    items = []
    if os.getenv("GOOGLE_BOOKS_API_KEY", "").strip():
        for variant in _query_variants(raw):
            found = _google_search(variant, max(limit, 10), italian=True)
            items.extend(book for item in found if (book := _google_book(item)))
            if len(items) >= max(limit * 2, 12):
                break
        if not items:
            for variant in _query_variants(raw)[:2]:
                found = _google_search(variant, max(limit, 10), italian=False)
                items.extend(book for item in found if (book := _google_book(item)))

    # Open Library integra Google Books e resta attivo anche senza la sua chiave.
    if len(items) < min(limit, 5):
        items.extend(_openlibrary_search(raw, max(limit, 10)))

    results = _merge(items, raw, limit)
    _cache_set(cache_key, results)
    return results


def search_recent_books(limit: int = 16):
    """Restituisce uscite recenti nei cataloghi, non annunci editoriali certificati."""
    if not os.getenv("GOOGLE_BOOKS_API_KEY", "").strip():
        return []
    cache_key = _cache_key("recent", str(limit) + "|" + ",".join(PUBLISHERS))
    cached = _cache_get(cache_key, hours=6)
    if cached is not None:
        return cached

    items = []
    per_publisher = max(3, min(8, (limit + len(PUBLISHERS) - 1) // len(PUBLISHERS)))
    current_year = datetime.now(timezone.utc).year
    for publisher in PUBLISHERS:
        found = _google_search(
            f'inpublisher:"{publisher}"', per_publisher, newest=True, italian=True
        )
        for item in found:
            book = _google_book(item)
            if not book or not book.get("italian_verified"):
                continue
            match = re.search(r"(19|20)\d{2}", str(book.get("published", "")))
            if not match:
                continue
            year = int(match.group(0))
            if current_year - 1 <= year <= current_year + 1:
                items.append(book)

    results = _merge(items, "novità editoriali italiane", limit)
    results.sort(key=lambda b: str(b.get("published", "")), reverse=True)
    _cache_set(cache_key, results)
    return results

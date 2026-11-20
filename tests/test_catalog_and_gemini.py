
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import book_catalog
import gemini_librarian


class CatalogTests(unittest.TestCase):

    def test_empty_search_returns_no_results(self):
        self.assertEqual(book_catalog.search_books(""), [])

    def test_isbn_is_recognized(self):
        title, author, isbn = book_catalog._parse_query(
            "9788806221234"
        )
        self.assertEqual(isbn, "9788806221234")
        self.assertEqual(title, "")
        self.assertEqual(author, "")

    def test_google_book_is_parsed(self):
        item = {
            "volumeInfo": {
                "title": "Il nome della rosa",
                "authors": ["Umberto Eco"],
                "publisher": "Bompiani",
                "publishedDate": "1980",
                "language": "it",
                "industryIdentifiers": [
                    {
                        "type": "ISBN_13",
                        "identifier": "9788845292613",
                    }
                ],
            }
        }

        book = book_catalog._google_book(item)

        self.assertEqual(book["title"], "Il nome della rosa")
        self.assertEqual(book["authors"], "Umberto Eco")
        self.assertTrue(book["italian_verified"])

    def test_merge_removes_duplicate_isbn(self):
        books = [
            {
                "title": "Libro",
                "authors": "Autore",
                "isbn": "9781234567890",
                "source": "Google Books",
                "italian_verified": True,
            },
            {
                "title": "Libro",
                "authors": "Autore",
                "isbn": "9781234567890",
                "source": "Open Library",
                "italian_verified": False,
            },
        ]

        result = book_catalog._merge(books, "Libro", 8)

        self.assertEqual(len(result), 1)
        self.assertIn("Google Books", result[0]["source"])
        self.assertIn("Open Library", result[0]["source"])


class GeminiTests(unittest.TestCase):

    def test_missing_api_key_returns_clear_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(
                gemini_librarian.GeminiLibrarianError
            ):
                gemini_librarian.generate_reply("Ciao")

    def test_generate_reply_uses_simulated_response(self):
        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Ti consiglio un bel romanzo."}
                        ]
                    }
                }
            ]
        }

        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "chiave-di-test"},
            clear=True,
        ):
            with patch.object(
                gemini_librarian.requests,
                "post",
                return_value=fake_response,
            ) as mocked_post:
                answer = gemini_librarian.generate_reply(
                    "Consigliami un libro"
                )

        self.assertEqual(
            answer,
            "Ti consiglio un bel romanzo."
        )
        mocked_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()

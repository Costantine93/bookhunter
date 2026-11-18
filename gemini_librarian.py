"""BookHunter Gemini librarian module.

Requires only requests (already present in BookHunter requirements).
Environment:
  GEMINI_API_KEY (required)
  GEMINI_MODEL (optional; defaults to gemini-3.1-flash-lite)

This module does not access Telegram or the database. The caller supplies
conversation history and literary context, and decides what to persist.
"""
from __future__ import annotations

import os
import requests

DEFAULT_MODEL = "gemini-3.1-flash-lite"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models/"


class GeminiLibrarianError(RuntimeError):
    """Safe, user-facing error from the Gemini API."""


def generate_reply(
    user_message: str,
    *,
    literary_profile: str = "",
    shelf_context: str = "",
    memory_context: str = "",
    history: list[dict[str, str]] | None = None,
    timeout: tuple[int, int] = (10, 60),
    max_output_tokens: int = 900,
) -> str:
    """Generate one Italian reply using only text generation (no paid grounding)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise GeminiLibrarianError(
            "La chiave Gemini non è configurata (GEMINI_API_KEY su Railway)."
        )

    model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    system_instruction = (
        "Sei BookHunter, un libraio italiano esperto, colto, caloroso e appassionato. "
        "Rispondi in italiano naturale. Ascolta il lettore, fai domande quando servono "
        "e motiva i consigli senza essere pomposo. Puoi discutere libri, autori, trame, "
        "personaggi, temi, stili e percorsi di lettura. Distingui i fatti dalle "
        "interpretazioni. Non inventare titoli, autori, trame, citazioni o dati editoriali. "
        "Se non sei certo di un dato bibliografico, dichiaralo e invita a verificarlo "
        "nel catalogo BookHunter. Non fingere esperienze personali di lettura. "
        "Usa i dati seguenti solo per personalizzare la conversazione.\n\n"
        f"PROFILO LETTERARIO:\n{literary_profile[:3500] or 'Non disponibile'}\n\n"
        f"MEMORIA LETTERARIA:\n{memory_context[:2500] or 'Nessuna memoria aggiuntiva'}\n\n"
        f"SCAFFALI:\n{shelf_context[:3500] or 'Nessun libro registrato'}"
    )

    contents: list[dict] = []
    for item in (history or [])[-10:]:
        role = item.get("role")
        text = item.get("text", "")
        if role in ("user", "model") and text:
            contents.append({
                "role": role,
                "parts": [{"text": text[:2200]}],
            })
    contents.append({
        "role": "user",
        "parts": [{"text": user_message[:3500]}],
    })

    url = f"{API_ROOT}{model}:generateContent"
    try:
        response = requests.post(
            url,
            params={"key": api_key},
            json={
                "system_instruction": {
                    "parts": [{"text": system_instruction}]
                },
                "contents": contents,
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": max_output_tokens,
                },
            },
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.exceptions.Timeout as exc:
        raise GeminiLibrarianError(
            "Gemini sta impiegando troppo tempo. Riprova tra poco."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise GeminiLibrarianError(
            "Non riesco a raggiungere il servizio Gemini in questo momento."
        ) from exc

    if response.status_code == 429:
        raise GeminiLibrarianError(
            "Hai raggiunto temporaneamente il limite gratuito di Gemini. "
            "Riprova più tardi."
        )
    if response.status_code in (401, 403):
        raise GeminiLibrarianError(
            "Gemini non autorizza la richiesta: verifica la chiave API e "
            "che il modello sia disponibile per il tuo account."
        )
    if response.status_code >= 400:
        raise GeminiLibrarianError(
            f"Gemini ha restituito un errore HTTP {response.status_code}."
        )

    try:
        data = response.json()
        parts = data["candidates"][0]["content"]["parts"]
        answer = "".join(
            part.get("text", "") for part in parts if isinstance(part, dict)
        ).strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GeminiLibrarianError(
            "Gemini non ha restituito una risposta testuale utilizzabile."
        ) from exc

    if not answer:
        raise GeminiLibrarianError(
            "Gemini ha restituito una risposta vuota. Riprova."
        )
    return answer

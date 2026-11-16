# BookHunter — assistente letterario personale Telegram

Bot Python privato per un solo utente. Gestisce scaffali, esclusioni di generi/autori, ricerca libri, suggerimenti AI opzionali e riepilogo settimanale.

## Configurazione

Variabili richieste:
- `TELEGRAM_BOT_TOKEN`: token di @BotFather
- `ALLOWED_TELEGRAM_USER_ID`: ID numerico del proprietario
- `DATABASE_PATH`: su Railway impostare `/data/bookhunter.sqlite3` e montare un Volume su `/data`

Facoltativa: `OPENAI_API_KEY` per le raccomandazioni motivate. La chiave API è distinta da un abbonamento ChatGPT.

## Comandi Telegram
- `/start` menu
- `/cerca <titolo/autore/tema>` ricerca catalografica
- `/consiglio [tema]` suggerimenti personalizzati
- `/wishlist <titolo>` aggiungi ai desideri
- `/lettura <titolo>` aggiungi a in lettura
- `/letto <titolo> | <voto 1-5>` registra un libro letto
- `/sospeso <titolo>` libro sospeso
- `/nonleggere <titolo>` esclude un titolo
- `/escludigenere <genere>` e `/includigenere <genere>` gestiscono i generi esclusi
- `/escludiautore <autore>` esclude un autore
- `/libreria [desidero|lettura|letti|sospesi|esclusi]`
- `/profilo`
- `/nuoveuscite`

I risultati Google Books sono metadati di catalogo e possono avere categorie incomplete. Le esclusioni automatiche basate su categorie/descrizioni sono un filtro euristico, non una classificazione infallibile. Le uscite editoriali non vengono inventate: `/nuoveuscite` funziona solo dopo aver configurato feed RSS ufficiali verificati.

## Deploy Railway
1. Creare un servizio da repository GitHub.
2. Impostare le variabili ambiente sopra indicate.
3. Aggiungere un Volume montato su `/data`.
4. Start command: `python main.py`.
5. Verificare i log e inviare `/start` al bot.

Non caricare mai `.env`, token Telegram o chiavi API nel repository. Prima dell'uso continuativo, verificare le fonti editoriali, configurare backup del volume e testare il bot in chat privata.

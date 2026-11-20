from __future__ import annotations
import logging, os, re, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
import requests
import feedparser
import time
from html.parser import HTMLParser
from urllib.parse import urljoin
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from gemini_librarian import generate_reply, extract_literary_memories, GeminiLibrarianError
from book_catalog import search_books as catalog_search_books
load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ALLOWED_ID = int(os.getenv('ALLOWED_TELEGRAM_USER_ID', '0') or 0)
DB_PATH = os.getenv('DATABASE_PATH', '/data/bookhunter.sqlite3')
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Rome')
RSS_FEEDS = [x.strip() for x in os.getenv('BOOK_RSS_FEEDS', '').split(',') if x.strip()]
PUBLISHERS = ['Adelphi','Einaudi','Sellerio','Feltrinelli','Mondadori','Neri Pozza','Iperborea','Fazi']
EXCLUDED_GENRES = []
DEFAULT_PROFILE = '''Profilo letterario BookHunter (italiano):
Autori apprezzati: John Steinbeck, Philip Roth, Han Kang, Stephen King, Donato Carrisi, Niccolò Ammaniti, Emmanuel Carrère.
Preferenze: romanzi autoconclusivi; narrativa letteraria e psicologica; autobiografia e autofiction; critica sociale; thriller, noir e horror.
Fantascienza: desidera riscoprirla, privilegiando romanzi autoconclusivi e non saghe lunghe.
Editori da monitorare: Adelphi, Einaudi, Sellerio, Feltrinelli, Mondadori, Neri Pozza, Iperborea, Fazi.
Nessuna esclusione automatica per genere o autore.
Preferisce recensioni ragionate e suggerimenti motivati. Lingua: italiano, preferire edizioni italiane.
'''

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('bookhunter')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS books (
 id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, authors TEXT DEFAULT '', publisher TEXT DEFAULT '',
 published TEXT DEFAULT '', description TEXT DEFAULT '', categories TEXT DEFAULT '', isbn TEXT DEFAULT '', link TEXT DEFAULT '',
 source TEXT DEFAULT '', status TEXT DEFAULT 'discovered', rating INTEGER, notes TEXT DEFAULT '', added_at TEXT NOT NULL,
 UNIQUE(title, authors));
CREATE TABLE IF NOT EXISTS exclusions (kind TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind,value));
CREATE TABLE IF NOT EXISTS seen_releases (id TEXT PRIMARY KEY, title TEXT, link TEXT, published TEXT, source TEXT, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS literary_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, memory TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
'''

def db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row; return con

def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        con.execute('INSERT OR IGNORE INTO profile(key,value) VALUES(?,?)', ('taste', DEFAULT_PROFILE))
        con.execute("DELETE FROM exclusions WHERE kind IN ('genre','author')")

def allowed(update: Update) -> bool:
    uid=update.effective_user.id if update.effective_user else None
    return ALLOWED_ID == 0 or uid == ALLOWED_ID
async def guard(update: Update) -> bool:
    if not allowed(update):
        if update.effective_message: await update.effective_message.reply_text('Questo bot è privato.')
        return False
    return True

def get_profile():
    with db() as con:
        row=con.execute("SELECT value FROM profile WHERE key='taste'").fetchone()
        profile = row['value'] if row else DEFAULT_PROFILE
    # Elimina dal vecchio profilo la riga di esclusioni predefinite, se presente.
    profile = '\n'.join(line for line in profile.splitlines() if not line.strip().casefold().startswith('generi esclusi:'))
    return profile

def exclusions(kind='genre'):
    with db() as con: return [r['value'] for r in con.execute('SELECT value FROM exclusions WHERE kind=? ORDER BY value',(kind,))]




def search_books(query: str, limit=8) -> list[dict[str, str]]:
    """Cerca opere disponibili in italiano su Open Library.

    La ricerca include sia opere di autori italiani sia opere tradotte:
    il filtro riguarda la lingua dell'edizione/catalogazione (ita), non
    la nazionalità dell'autore.
    """
    raw_query = query.strip()
    if not raw_query:
        return []

    known_authors = [
        'Emmanuel Carrère', 'John Steinbeck', 'Philip Roth', 'Han Kang',
        'Stephen King', 'Donato Carrisi', 'Niccolò Ammaniti'
    ]

    title = ''
    author = ''

    title_match = re.search(
        r'(?:^|\s)(?:titolo|title)\s*:\s*(.+?)(?=\s+(?:autore|author)\s*:|$)',
        raw_query, flags=re.IGNORECASE
    )
    author_match = re.search(
        r'(?:^|\s)(?:autore|author)\s*:\s*(.+?)(?=\s+(?:titolo|title)\s*:|$)',
        raw_query, flags=re.IGNORECASE
    )

    if title_match:
        title = title_match.group(1).strip().strip('"')
    if author_match:
        author = author_match.group(1).strip().strip('"')

    if not title and not author:
        folded = raw_query.casefold()
        for candidate in sorted(known_authors, key=len, reverse=True):
            pos = folded.rfind(candidate.casefold())
            if pos >= 0:
                before = raw_query[:pos].strip()
                after = raw_query[pos + len(candidate):].strip()
                if not after:
                    title, author = before, candidate
                    break
                if not before:
                    author, title = candidate, after
                    break

    # Open Library usa codici ISO 639-2/B: ita = italiano.
    # La clausola language:ita vale anche per autori stranieri tradotti.
    query_parts = []
    if title:
        query_parts.append(f'title:"{title}"')
    if author:
        query_parts.append(f'author:"{author}"')
    if not title and not author:
        query_parts.append(raw_query)
    query_parts.append('language:ita')

    base_query = ' '.join(query_parts[:-1])
    fields = (
        'key,title,author_name,first_publish_year,'
        'publisher,subject,isbn,cover_i,first_sentence,language,'
        'editions,editions.key,editions.title,editions.language'
    )

    def fetch(query_text):
        params = {
            'q': query_text,
            'limit': min(max(int(limit), 1), 20),
            'fields': fields,
            'lang': 'it'
        }
        last_error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    'https://openlibrary.org/search.json',
                    params=params,
                    headers={'User-Agent': 'BookHunterTelegramBot/1.0 (book search)'},
                    timeout=(10, 30)
                )
                response.raise_for_status()
                return response.json()
            except (requests.exceptions.RequestException, ValueError) as exc:
                last_error = exc
                log.warning(
                    'Open Library tentativo %s/3 non riuscito: %s',
                    attempt + 1, exc
                )
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        log.error('Ricerca Open Library fallita dopo 3 tentativi')
        raise last_error

    # Prima cerchiamo le edizioni catalogate in italiano.
    data = fetch(base_query + ' language:ita')
    docs = data.get('docs', [])
    # Il campo language dell'opera può contenere lingue di edizioni diverse.
    # Per verificare davvero l'italiano controlliamo la lingua della singola
    # edizione restituita nel blocco editions.

    # Completiamo con risultati non filtrati per lingua, così da non perdere
    # opere che potrebbero avere un'edizione italiana non catalogata. Questi
    # risultati vengono marcati come non verificati e ordinati in fondo.
    if len(docs) < min(max(int(limit), 1), 20):
        fallback = fetch(base_query).get('docs', [])
        known_keys = {item.get('key') for item in docs}
        docs.extend(item for item in fallback if item.get('key') not in known_keys)

    results = []
    for item in docs:
        edition_data = item.get('editions') or {}
        edition_docs = edition_data.get('docs') or []
        italian_editions = [
            edition for edition in edition_docs
            if 'ita' in (edition.get('language') or [])
        ]
        italian_verified = bool(italian_editions)

        # Mostra il titolo della specifica edizione italiana, non il titolo
        # generale dell'opera che spesso è nella lingua originale.
        edition_title = next(
            (edition.get('title') for edition in italian_editions if edition.get('title')),
            ''
        )

        sentences = item.get('first_sentence') or []
        if isinstance(sentences, list):
            description = ' '.join(str(s) for s in sentences)
        elif isinstance(sentences, str):
            description = sentences
        else:
            description = ''

        key = item.get('key', '')
        results.append({
            'title': edition_title or item.get('title') or 'Titolo non disponibile',
            'authors': ', '.join(item.get('author_name') or []),
            'publisher': ', '.join((item.get('publisher') or [])[:3]),
            'published': str(item.get('first_publish_year', '')),
            'description': description[:1200],
            'categories': ', '.join((item.get('subject') or [])[:10]),
            'isbn': next(iter(item.get('isbn') or []), ''),
            'link': 'https://openlibrary.org' + key if key else '',
            'source': 'Open Library',
            'italian_verified': italian_verified
        })

    results.sort(key=lambda book: not book.get('italian_verified', False))
    return results


def search_books(query: str, limit=8) -> list[dict[str, str]]:
    """Cerca libri nei cataloghi Google Books e Open Library."""
    try:
        return catalog_search_books(query, limit=limit)
    except Exception as exc:
        log.exception("Errore durante la ricerca nel catalogo libri: %s", exc)
        return []
def blocked(book: dict[str, str]) -> bool:
    return False

def save_book(b,status='wishlist',rating=None):
    with db() as con:
        con.execute('''INSERT INTO books(title,authors,publisher,published,description,categories,isbn,link,source,status,rating,added_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(title,authors) DO UPDATE SET status=CASE WHEN books.status='discovered' THEN excluded.status ELSE books.status END, rating=COALESCE(excluded.rating,books.rating)''',
        (b.get('title',''),b.get('authors',''),b.get('publisher',''),b.get('published',''),b.get('description',''),b.get('categories',''),b.get('isbn',''),b.get('link',''),b.get('source','manual'),status,rating,datetime.now(timezone.utc).isoformat()))

def find_saved(title):
    with db() as con: return con.execute('SELECT * FROM books WHERE title LIKE ? ORDER BY id DESC LIMIT 1',(f'%{title}%',)).fetchone()

def fmt(b, n=None):
    prefix = f"{n}. " if n else ""
    lines = [f"📚 {prefix}{b.get('title', 'Titolo non disponibile')}"]
    if b.get('authors'):
        lines.append('Autore: ' + b['authors'])
    if b.get('publisher'):
        lines.append('Editore: ' + b['publisher'])
    if b.get('published'):
        lines.append('Pubblicazione: ' + b['published'])
    if b.get('categories'):
        lines.append('Categorie: ' + b['categories'])
    if b.get('link'):
        lines.append('Scheda libro: ' + b['link'])
    lines.append('Lingua italiana: verificata' if b.get('italian_verified') else 'Lingua italiana: non verificata')
    return '\n'.join(lines)


def book_keyboard(b):
    # Callback data stores the exact title; IDs are stable database IDs after saving.
    with db() as con:
        row=con.execute('SELECT id FROM books WHERE title=? AND authors=?',(b.get('title',''),b.get('authors',''))).fetchone()
    bid=row['id'] if row else 0
    return InlineKeyboardMarkup([[InlineKeyboardButton('❤️ Desidero leggere',callback_data=f's:{bid}:wishlist'),InlineKeyboardButton('📖 In lettura',callback_data=f's:{bid}:reading')],
      [InlineKeyboardButton('✅ Letto',callback_data=f's:{bid}:read'),InlineKeyboardButton('🚫 Non mi interessa',callback_data=f's:{bid}:rejected')]])

async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text('📚 Benvenuto in BookHunter!\n\nIl tuo libraio personale: cerca libri, gestisci i tuoi scaffali, ricevi consigli personalizzati e conversa liberamente con il libraio AI.',reply_markup=InlineKeyboardMarkup([
      [InlineKeyboardButton('✨ Consigliami libri',callback_data='menu:recommend'),InlineKeyboardButton('🔎 Cerca libri',callback_data='menu:search')],
      [InlineKeyboardButton('❤️ Desidero leggere',callback_data='menu:wishlist'),InlineKeyboardButton('✅ Libri letti',callback_data='menu:library')],
      [InlineKeyboardButton('📖 In lettura',callback_data='menu:reading'),InlineKeyboardButton('👤 Il mio profilo',callback_data='menu:profile')],
      [InlineKeyboardButton('🆕 Nuove uscite',callback_data='menu:releases')]]))

async def help_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text('/cerca <titolo, autore o tema>\n/consiglio [tema]\n/wishlist <titolo>\n/lettura <titolo>\n/letto <titolo> | <voto 1-5>\n/sospeso <titolo>\n/nonleggere <titolo>\n/libreria [desidero|lettura|letti|sospesi|esclusi]\n/profilo\n/memoria\n/cancellamemoria\n/nuoveuscite')

async def search_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    q=' '.join(context.args).strip()
    if not q: return await update.message.reply_text('Esempio: /cerca thriller psicologico. Mostro edizioni in italiano, anche di autori stranieri tradotti.')
    try:
        books=search_books(q,10)
        if not books: return await update.message.reply_text('Nessun risultato trovato nel catalogo. Prova un titolo, autore o argomento più specifico.')
        for i,b in enumerate(books[:6],1):
            save_book(b,'discovered')
            await update.message.reply_text(fmt(b,i), reply_markup=book_keyboard(b), disable_web_page_preview=True)
    except Exception as e:
        log.exception('search failed'); await update.message.reply_text(f'Ricerca non disponibile ({type(e).__name__}).')

async def recommendation_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    """Consigli personalizzati senza servizi AI a pagamento: ranking locale + Open Library."""
    if not await guard(update): return
    message=update.effective_message
    topic=' '.join(context.args).strip()
    with db() as con:
        read_rows=con.execute("SELECT title,authors,publisher,categories,rating FROM books WHERE status='read' ORDER BY id DESC LIMIT 40").fetchall()
        wish_rows=con.execute("SELECT title,authors,publisher,categories FROM books WHERE status='wishlist' ORDER BY id DESC LIMIT 40").fetchall()
        reading_rows=con.execute("SELECT title,authors FROM books WHERE status='reading' ORDER BY id DESC LIMIT 20").fetchall()
        rejected_rows=con.execute("SELECT title,authors FROM books WHERE status='rejected' ORDER BY id DESC LIMIT 60").fetchall()
        known_rows=con.execute("SELECT title,authors FROM books WHERE status IN ('read','wishlist','reading','rejected')").fetchall()

    def norm(value):
        return re.sub(r'[^a-z0-9àèéìòù]+',' ',(value or '').casefold()).strip()
    known={norm(r['title']) for r in known_rows}
    known_authors={norm(r['authors']) for r in known_rows if r['authors']}
    # I voti alti pesano di più; i libri desiderati contribuiscono come interessi, non come letture concluse.
    liked_authors=[]
    for r in read_rows:
        if r['authors'] and (r['rating'] is None or r['rating']>=4):
            liked_authors.extend(a.strip() for a in r['authors'].split(',') if a.strip())
    liked_authors += [r['authors'] for r in wish_rows if r['authors']]
    author_queries=[]
    for author in liked_authors:
        if author and author not in author_queries: author_queries.append(author)
    # Un tema digitato dall'utente è una ricerca aggiuntiva, non sostituisce la storia personale.
    queries=[]
    if topic: queries.append(topic)
    queries.extend(f'author:"{a}"' for a in author_queries[:5])
    if not queries:
        queries=['literary fiction Italian translation','psychological fiction Italian','autobiography Italian']
    candidates=[]; seen=set()
    try:
        for query in queries[:6]:
            for book in search_books(query,12):
                title=norm(book.get('title'))
                if not title or title in known or title in seen: continue
                if not book.get('italian_verified'): continue
                seen.add(title); candidates.append(book)
        if not candidates:
            return await message.reply_text('Non ho trovato nuovi titoli italiani abbastanza pertinenti. Prova una ricerca più specifica o registra qualche libro letto e i relativi voti.')

        # Punteggio deterministico e trasparente: affinità autore/categorie e preferenze del profilo.
        liked_author_tokens={norm(a) for a in author_queries}
        liked_category_text=' '.join(norm(r['categories']) for r in read_rows if r['rating'] is None or r['rating']>=4)
        wish_category_text=' '.join(norm(r['categories']) for r in wish_rows)
        profile_text=norm(get_profile())
        for book in candidates:
            author=norm(book.get('authors'))
            cats=norm(book.get('categories'))
            score=0
            if author and any(author==a or author in a or a in author for a in liked_author_tokens): score+=5
            score+=sum(1 for token in set(cats.split()) if len(token)>4 and token in liked_category_text.split())
            score+=sum(1 for token in set(cats.split()) if len(token)>4 and token in wish_category_text.split())
            score+=sum(1 for token in ('narrativa','fiction','literary','psychological','thriller','autobiography','horror','fantasy') if token in cats or token in profile_text and token in cats)
            if book.get('italian_verified'): score+=2
            book['_score']=score
        candidates.sort(key=lambda b:(-b['_score'], norm(b.get('title'))))
        picks=candidates[:5]
        intro='✨ **Consigli personalizzati – modalità gratuita**\nHo confrontato i tuoi scaffali con il catalogo Open Library. I suggerimenti sono selezionati con un sistema di affinità, senza API AI a pagamento.'
        await message.reply_text(intro,parse_mode='Markdown')
        for i,book in enumerate(picks,1):
            # Salva il suggerimento prima di creare i pulsanti: così i callback
            # puntano a un ID reale e non allo zero.
            save_book(book, 'discovered')
            reasons=[]
            if any(norm(book.get('authors'))==norm(a) for a in author_queries): reasons.append('autore che hai già letto o inserito nei desideri')
            if book.get('_score',0)>2: reasons.append('categorie affini ai tuoi interessi registrati')
            if not reasons: reasons.append('corrispondenza con il profilo letterario e disponibilità di un’edizione italiana catalogata')
            await message.reply_text(fmt(book,i)+'\n\nPerché te lo consiglio: '+', '.join(reasons)+'.',reply_markup=book_keyboard(book),disable_web_page_preview=True)
    except Exception as e:
        log.exception('free recommendation failed')
        await message.reply_text(f'Consigli non disponibili in questo momento ({type(e).__name__}). Nessun servizio a pagamento è stato contattato; riprova quando il catalogo è raggiungibile.')


def literary_context():
    """Prepara profilo, scaffali, memoria e cronologia per il libraio AI."""
    with db() as con:
        books = con.execute(
            "SELECT title,authors,status,rating FROM books "
            "WHERE status IN ('read','wishlist','reading') ORDER BY id DESC LIMIT 35"
        ).fetchall()
        memories = con.execute(
            "SELECT memory FROM literary_memory ORDER BY id DESC LIMIT 30"
        ).fetchall()
        history = con.execute(
            "SELECT role,content FROM chat_history ORDER BY id DESC LIMIT 10"
        ).fetchall()

    shelf_lines = []
    labels = {'read': 'letto', 'wishlist': 'desiderato', 'reading': 'in lettura'}
    for row in books:
        rating = f", voto {row['rating']}/5" if row['rating'] else ''
        shelf_lines.append(
            f"- {row['title']} — {row['authors']} "
            f"({labels.get(row['status'], row['status'])}{rating})"
        )
    memory_text = '\n'.join(f"- {row['memory']}" for row in memories)
    shelf_text = '\n'.join(shelf_lines)
    recent = [
        {'role': row['role'], 'text': row['content']}
        for row in reversed(history)
    ]
    return get_profile(), shelf_text, memory_text, recent


async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i messaggi testuali liberi con Gemini."""
    if not await guard(update):
        return
    message = update.effective_message
    user_text = (message.text or '').strip() if message else ''
    if not user_text:
        return
    if not os.getenv('GEMINI_API_KEY', '').strip():
        await message.reply_text(
            "Il libraio AI non è ancora configurato. Verifica GEMINI_API_KEY su Railway."
        )
        return

    profile, shelves, memories, history = literary_context()
    try:
        answer = generate_reply(
            user_text,
            literary_profile=profile,
            shelf_context=shelves,
            memory_context=memories,
            history=history,
        )
        now = datetime.now(timezone.utc).isoformat()
        with db() as con:
            con.execute(
                "INSERT INTO chat_history(role,content,created_at) VALUES('user',?,?)",
                (user_text[:3500], now)
            )
            con.execute(
                "INSERT INTO chat_history(role,content,created_at) VALUES('model',?,?)",
                (answer[:6000], now)
            )
            con.execute(
                "DELETE FROM chat_history WHERE id NOT IN "
                "(SELECT id FROM chat_history ORDER BY id DESC LIMIT 30)"
            )
            user_turns = con.execute(
                "SELECT COUNT(*) AS n FROM chat_history WHERE role='user'"
            ).fetchone()['n']
        # Estrazione periodica: limita le chiamate aggiuntive e salva solo gusti espliciti.
        if user_turns % 4 == 0:
            extracted = extract_literary_memories(user_text)
            if extracted:
                with db() as con:
                    for item in extracted:
                        con.execute(
                            "INSERT OR IGNORE INTO literary_memory(memory,created_at) VALUES(?,?)",
                            (item, datetime.now(timezone.utc).isoformat())
                        )
                    con.execute(
                        "DELETE FROM literary_memory WHERE id NOT IN "
                        "(SELECT id FROM literary_memory ORDER BY id DESC LIMIT 40)"
                    )
        for start in range(0, len(answer), 3900):
            await message.reply_text(answer[start:start + 3900])
    except GeminiLibrarianError as exc:
        await message.reply_text(str(exc))
    except Exception:
        log.exception('Gemini chat error')
        await message.reply_text(
            "Il libraio AI ha avuto un problema inatteso. Riprova tra poco."
        )


async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    with db() as con:
        rows = con.execute(
            "SELECT memory FROM literary_memory ORDER BY id DESC LIMIT 30"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text(
            "🧠 Non ho ancora memorie letterarie salvate. "
            "Parlando di libri e preferenze, potrò costruire il tuo profilo."
        )
        return
    text = "🧠 **Memoria letteraria di BookHunter**\n\n" + '\n'.join(
        f"• {row['memory']}" for row in rows
    )
    await update.effective_message.reply_text(text, parse_mode='Markdown')


async def clear_memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    with db() as con:
        con.execute("DELETE FROM literary_memory")
        con.execute("DELETE FROM chat_history")
    await update.effective_message.reply_text(
        "Ho cancellato la memoria letteraria e la cronologia della chat di BookHunter."
    )

def saved_titles():
    with db() as con: return '; '.join(r['title'] for r in con.execute("SELECT title FROM books WHERE status IN ('wishlist','reading','read','rejected')"))

async def add_status(update,context,status):
    if not await guard(update): return
    raw=' '.join(context.args).strip(); rating=None
    if status=='read' and '|' in raw:
        raw,val=[x.strip() for x in raw.split('|',1)]
        try: rating=max(1,min(5,int(val)))
        except ValueError: pass
    if not raw: return await update.message.reply_text('Indica il titolo del libro.')
    try:
        res=search_books('intitle:'+raw,1); b=res[0] if res else {'title':raw,'authors':'','source':'manuale'}
        save_book(b,status,rating); await update.message.reply_text(f"Salvato in '{status}': {b['title']}")
    except Exception as e:
        log.exception('add status failed'); await update.message.reply_text(f'Non sono riuscito a cercare il libro ({type(e).__name__}); puoi riprovare.')
async def wishlist_cmd(u,c): await add_status(u,c,'wishlist')
async def reading_cmd(u,c): await add_status(u,c,'reading')
async def read_cmd(u,c): await add_status(u,c,'read')
async def suspended_cmd(u,c): await add_status(u,c,'suspended')
async def rejected_cmd(u,c): await add_status(u,c,'rejected')

async def callback(update:Update,context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not await guard(update): return
    await q.answer()
    data=q.data or ''
    if data.startswith('s:'):
        _,bid,status=data.split(':',2)
        with db() as con:
            if status=='rejected': con.execute("UPDATE books SET status='rejected' WHERE id=?",(int(bid),))
            else: con.execute('UPDATE books SET status=? WHERE id=?',(status,int(bid)))
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text({'wishlist':'Aggiunto a ❤️ Desidero leggere.','reading':'Spostato in 📖 In lettura.','read':'Spostato in ✅ Letti.','rejected':'Ricevuto: non te lo proporrò più.'}[status])
    elif data=='menu:recommend': await recommendation_cmd(update,context)
    elif data=='menu:search': await q.message.reply_text('Scrivi /cerca seguito da autore, titolo o argomento.')
    elif data=='menu:library': await show_library(q.message, 'read')
    elif data=='menu:wishlist': await show_library(q.message, 'wishlist')
    elif data=='menu:reading': await show_library(q.message, 'reading')
    elif data=='menu:releases': await releases_cmd_from_message(q.message)
    elif data=='menu:profile': await q.message.reply_text(get_profile()[:3900])

async def show_library(message, status=None):
    with db() as con:
        if status=='esclusi':
            rows=con.execute("SELECT id,title,authors,status,rating FROM books WHERE status='rejected' ORDER BY id DESC LIMIT 30").fetchall()
        elif status:
            rows=con.execute('SELECT id,title,authors,status,rating FROM books WHERE status=? ORDER BY id DESC LIMIT 30',(status,)).fetchall()
        else:
            rows=con.execute('SELECT id,title,authors,status,rating FROM books ORDER BY id DESC LIMIT 30').fetchall()
    labels={'wishlist':'❤️ Desidero leggere','reading':'📖 In lettura','read':'✅ Libri letti','suspended':'⏸ Sospesi','rejected':'🚫 Esclusi','discovered':'Scoperti'}
    heading=labels.get(status, '📚 La tua libreria')
    if not rows:
        return await message.reply_text(f'{heading}\n\nQuesto scaffale è vuoto.')
    await message.reply_text(f'{heading} — {len(rows)} libri')
    for r in rows:
        line=f"📚 {r['title']} — {r['authors']}"
        if r['rating']:
            line+=f" | {r['rating']}/5"
        if status=='wishlist':
            keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('📖 In lettura',callback_data=f"s:{r['id']}:reading"),InlineKeyboardButton('✅ Letto',callback_data=f"s:{r['id']}:read")]])
        elif status=='reading':
            keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Letto',callback_data=f"s:{r['id']}:read")]])
        else:
            keyboard=None
        await message.reply_text(line,reply_markup=keyboard)

async def library_cmd(update,context):
    if not await guard(update): return
    key=' '.join(context.args).strip().casefold(); mapping={'desidero':'wishlist','lettura':'reading','letti':'read','sospesi':'suspended','esclusi':'esclusi'}
    await show_library(update.message,mapping.get(key))

async def profile_cmd(update,context):
    if not await guard(update): return
    await update.message.reply_text(get_profile()+'\n\nNessuna esclusione automatica per genere o autore.')

class ReleaseLinkParser(HTMLParser):
    """Raccoglie i link alle comunicazioni ufficiali sulle novità editoriali."""
    def __init__(self):
        super().__init__(); self.links=[]; self._href=None; self._text=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=='a':
            self._href=dict(attrs).get('href'); self._text=[]
    def handle_data(self, data):
        if self._href is not None: self._text.append(data)
    def handle_endtag(self, tag):
        if tag.lower()=='a' and self._href is not None:
            title=' '.join(' '.join(self._text).split())
            if title: self.links.append((title,self._href))
            self._href=None; self._text=[]

async def releases_cmd_from_message(message):
    """Mostra avvisi ufficiali di novità in libreria; non confonde ristampe e prime uscite."""
    items=[]
    # Fonte ufficiale del distributore: le schede riportano finestre di arrivo e link ai lanci.
    official_url='https://ml-portale-prod-cms.messaggerielibri.it/'
    try:
        response=requests.get(official_url,headers={'User-Agent':'BookHunter/1.0'},timeout=(10,25))
        response.raise_for_status()
        parser=ReleaseLinkParser(); parser.feed(response.text)
        seen=set()
        for title,href in parser.links:
            low=title.casefold()
            if ('novità in libreria' in low or 'fumetti novità' in low) and href:
                link=urljoin(official_url,href)
                if link not in seen:
                    seen.add(link); items.append((title,link,'Messaggerie Libri'))
            if len(items)>=8: break
    except Exception as e:
        log.warning('Messaggerie novita non raggiungibile: %s',e)
    # Eventuali feed aggiuntivi configurati dall'utente su Railway.
    for url in RSS_FEEDS:
        try:
            feed=feedparser.parse(url)
            for entry in feed.entries[:5]:
                title=entry.get('title','Senza titolo')
                link=entry.get('link','')
                if link and not any(existing[1]==link for existing in items):
                    published=entry.get('published',entry.get('updated','Data non indicata'))
                    items.append((f'{title} — {published}',link,feed.feed.get('title','Feed editoriale')))
        except Exception as e:
            log.warning('Feed novita non disponibile %s: %s',url,e)
    if not items:
        return await message.reply_text('Non riesco a raggiungere in questo momento le fonti editoriali. Riprova più tardi; non mostro titoli non verificati.')
    await message.reply_text('🆕 **Nuovi arrivi e novità editoriali**\nFonte principale: Messaggerie Libri. Le schede distinguono le novità dalle ristampe.',parse_mode='Markdown')
    for title,link,source in items[:10]:
        await message.reply_text(f'📚 {title}\nFonte: {source}\n{link}',disable_web_page_preview=True)

async def releases_cmd(update,context):
    if not await guard(update): return
    await releases_cmd_from_message(update.message)

async def weekly_digest(context:ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_ID: return
    with db() as con: rows=con.execute("SELECT title,authors,publisher,published FROM books WHERE status='discovered' ORDER BY id DESC LIMIT 5").fetchall()
    text='📚 Il tuo riepilogo BookHunter\n\n'
    if rows: text+='\n'.join(f"• {r['title']} — {r['authors']} ({r['publisher']}, {r['published']})" for r in rows)
    else: text+='Nessuna nuova scoperta salvata. Prova /consiglio.'
    await context.bot.send_message(chat_id=ALLOWED_ID,text=text)
async def post_init(app:Application):
    app.job_queue.run_daily(weekly_digest,time=datetime.now(ZoneInfo(TIMEZONE)).replace(hour=10,minute=0,second=0,microsecond=0).timetz(),days=(6,),name='weekly-digest')

def main():
    if not TOKEN: raise SystemExit('Configura TELEGRAM_BOT_TOKEN')
    if ALLOWED_ID==0: log.warning('ALLOWED_TELEGRAM_USER_ID non impostato: bot non limitato a un solo utente')
    init_db(); app=Application.builder().token(TOKEN).post_init(post_init).build()
    handlers=[CommandHandler('start',start),CommandHandler(['aiutami','help'],help_cmd),CommandHandler('cerca',search_cmd),CommandHandler('consiglio',recommendation_cmd),CommandHandler('wishlist',wishlist_cmd),CommandHandler('lettura',reading_cmd),CommandHandler('letto',read_cmd),CommandHandler('sospeso',suspended_cmd),CommandHandler('nonleggere',rejected_cmd),CommandHandler('libreria',library_cmd),CommandHandler('profilo',profile_cmd),CommandHandler('nuoveuscite',releases_cmd),CommandHandler('memoria',memory_cmd),CommandHandler('cancellamemoria',clear_memory_cmd),MessageHandler(filters.TEXT & ~filters.COMMAND,chat_cmd),CallbackQueryHandler(callback)]
    for h in handlers: app.add_handler(h)
    log.info('BookHunter avviato'); app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__': main()

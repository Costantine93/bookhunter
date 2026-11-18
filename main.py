from __future__ import annotations
import logging, os, re, sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
import requests
import feedparser
import time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
ALLOWED_ID = int(os.getenv('ALLOWED_TELEGRAM_USER_ID', '0') or 0)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.1')
DB_PATH = os.getenv('DATABASE_PATH', '/data/bookhunter.sqlite3')
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Rome')
RSS_FEEDS = [x.strip() for x in os.getenv('BOOK_RSS_FEEDS', '').split(',') if x.strip()]
PUBLISHERS = ['Adelphi','Einaudi','Sellerio','Feltrinelli','Mondadori','Neri Pozza','Iperborea','Fazi']
EXCLUDED_GENRES = ['Romance','Young Adult','Letteratura erotica','Poesia']
DEFAULT_PROFILE = '''Profilo letterario BookHunter (italiano):
Autori apprezzati: John Steinbeck, Philip Roth, Han Kang, Stephen King, Donato Carrisi, Niccolò Ammaniti, Emmanuel Carrère.
Preferenze: romanzi autoconclusivi; narrativa letteraria e psicologica; autobiografia e autofiction; critica sociale; thriller, noir e horror.
Fantascienza: desidera riscoprirla, privilegiando romanzi autoconclusivi e non saghe lunghe.
Editori da monitorare: Adelphi, Einaudi, Sellerio, Feltrinelli, Mondadori, Neri Pozza, Iperborea, Fazi.
Generi esclusi: Romance, Young Adult, Letteratura erotica, Poesia.
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
'''

def db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    con=sqlite3.connect(DB_PATH); con.row_factory=sqlite3.Row; return con

def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        con.execute('INSERT OR IGNORE INTO profile(key,value) VALUES(?,?)', ('taste', DEFAULT_PROFILE))
        for g in EXCLUDED_GENRES: con.execute('INSERT OR IGNORE INTO exclusions(kind,value) VALUES(?,?)', ('genre',g))

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
        return row['value'] if row else DEFAULT_PROFILE

def exclusions(kind='genre'):
    with db() as con: return [r['value'] for r in con.execute('SELECT value FROM exclusions WHERE kind=? ORDER BY value',(kind,))]




def search_books(query: str, limit=8) -> list[dict[str, str]]:
    """Ricerca libri per titolo, autore o testo libero."""

    raw_query = query.strip()
    if not raw_query:
        return []

    known_authors = [
        'Emmanuel Carrère',
        'John Steinbeck',
        'Philip Roth',
        'Han Kang',
        'Stephen King',
        'Donato Carrisi',
        'Niccolò Ammaniti'
    ]

    title = ''
    author = ''

    title_match = re.search(
        r'(?:^|\s)(?:titolo|title)\s*:\s*(.+?)(?=\s+(?:autore|author)\s*:|$)',
        raw_query,
        flags=re.IGNORECASE
    )

    author_match = re.search(
        r'(?:^|\s)(?:autore|author)\s*:\s*(.+?)(?=\s+(?:titolo|title)\s*:|$)',
        raw_query,
        flags=re.IGNORECASE
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
                    title = before
                    author = candidate
                    break

                if not before:
                    author = candidate
                    title = after
                    break

    params = {
        'limit': min(max(int(limit), 1), 20),
        'fields': (
            'key,title,author_name,first_publish_year,'
            'publisher,subject,isbn,cover_i,first_sentence'
        )
    }

    if title and author:
        params.update({
            'title': title,
            'author': author
        })
    elif author:
        params['author'] = author
    elif title:
        params['title'] = title
    else:
        params['q'] = raw_query

    try:
        for attempt in range(3):
            try:
                r = requests.get(
                    'https://openlibrary.org/search.json',
                    params=params,
                    headers={
                        'User-Agent': 'BookHunterTelegramBot/1.0'
                    },
                    timeout=(10, 30)
                )
                break

            except requests.exceptions.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2)

        if not r.ok:
            log.error(
                'Open Library HTTP %s: %s',
                r.status_code,
                r.text[:1000]
            )

        r.raise_for_status()

        data = r.json()
        out = []

        for item in data.get('docs', []):
            title_value = item.get('title') or 'Titolo non disponibile'

            authors = ', '.join(item.get('author_name') or [])
            publishers = ', '.join((item.get('publisher') or [])[:3])
            subjects = ', '.join((item.get('subject') or [])[:10])

            isbn = next(iter(item.get('isbn') or []), '')

            sentences = item.get('first_sentence', [])

            if isinstance(sentences, list):
                description = ' '.join(str(s) for s in sentences)
            elif isinstance(sentences, str):
                description = sentences
            else:
                description = ''

            key = item.get('key', '')
            link = (
                'https://openlibrary.org' + key
                if key else ''
            )

            out.append({
                'title': title_value,
                'authors': authors,
                'publisher': publishers,
                'published': str(item.get('first_publish_year', '')),
                'description': description[:1200],
                'categories': subjects,
                'isbn': isbn,
                'link': link,
                'source': 'Open Library'
            })

        return out

    except requests.exceptions.RequestException as e:
        log.exception('Open Library search failed: %s', e)
        raise

    except (ValueError, TypeError, KeyError) as e:
        log.exception('Open Library response error: %s', e)
        raise



def blocked(book: dict[str, str]) -> bool:
    return False

def save_book(b,status='wishlist',rating=None):
    with db() as con:
        con.execute('''INSERT INTO books(title,authors,publisher,published,description,categories,isbn,link,source,status,rating,added_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(title,authors) DO UPDATE SET status=excluded.status, rating=COALESCE(excluded.rating,books.rating)''',
        (b.get('title',''),b.get('authors',''),b.get('publisher',''),b.get('published',''),b.get('description',''),b.get('categories',''),b.get('isbn',''),b.get('link',''),b.get('source','manual'),status,rating,datetime.now(timezone.utc).isoformat()))

def find_saved(title):
    with db() as con: return con.execute('SELECT * FROM books WHERE title LIKE ? ORDER BY id DESC LIMIT 1',(f'%{title}%',)).fetchone()

def fmt(b,n=None):
    p=f"{n}. " if n else ''
    lines=[f"📚 *{p}{b.get('title','Titolo non disponibile')}*"]
    if b.get('authors'): lines.append('Autore: '+b['authors'])
    if b.get('publisher'): lines.append('Editore: '+b['publisher'])
    if b.get('published'): lines.append('Pubblicazione: '+b['published'])
    if b.get('categories'): lines.append('Categorie: '+b['categories'])
    if b.get('link'): lines.append('[Scheda libro]('+b['link']+')')
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
    await update.message.reply_text('📚 Benvenuto in BookHunter!\n\nSono il tuo libraio personale. Usa i pulsanti qui sotto o /aiutami per i comandi.',reply_markup=InlineKeyboardMarkup([
      [InlineKeyboardButton('✨ Consigliami libri',callback_data='menu:recommend'),InlineKeyboardButton('🔎 Cerca libri',callback_data='menu:search')],
      [InlineKeyboardButton('📚 La mia libreria',callback_data='menu:library'),InlineKeyboardButton('🚫 Generi esclusi',callback_data='menu:genres')],
      [InlineKeyboardButton('🆕 Nuove uscite',callback_data='menu:releases'),InlineKeyboardButton('👤 Il mio profilo',callback_data='menu:profile')]]))

async def help_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    await update.message.reply_text('/cerca <titolo, autore o tema>\n/consiglio [tema]\n/wishlist <titolo>\n/lettura <titolo>\n/letto <titolo> | <voto 1-5>\n/sospeso <titolo>\n/nonleggere <titolo>\n/escludigenere <genere>\n/includigenere <genere>\n/escludiautore <autore>\n/libreria [desidero|lettura|letti|sospesi|esclusi]\n/profilo\n/nuoveuscite')

async def search_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    q=' '.join(context.args).strip()
    if not q: return await update.message.reply_text('Esempio: /cerca thriller psicologico italiano')
    try:
        books=[b for b in search_books(q,10) if not blocked(b)]
        if not books: return await update.message.reply_text('Nessun risultato compatibile con i filtri attuali. Puoi modificare le esclusioni con /includigenere.')
        for i,b in enumerate(books[:6],1):
            save_book(b,'discovered')
            await update.message.reply_text(fmt(b,i),parse_mode='Markdown',reply_markup=book_keyboard(b),disable_web_page_preview=True)
    except Exception as e:
        log.exception('search failed'); await update.message.reply_text(f'Ricerca non disponibile ({type(e).__name__}).')

async def recommendation_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not await guard(update): return
    topic=' '.join(context.args).strip() or 'romanzi autoconclusivi di narrativa letteraria, autobiografia, critica sociale, thriller e fantascienza autoconclusiva'
    query=topic+' (John Steinbeck OR Philip Roth OR Han Kang OR Stephen King OR Donato Carrisi OR Emmanuel Carrere)'
    try:
        books=[b for b in search_books(query,20) if not blocked(b)]
        if not books: return await update.message.reply_text('Non ho trovato risultati compatibili. Prova un tema più specifico.')
        for b in books[:10]: save_book(b,'discovered')
        if OPENAI_API_KEY:
            from openai import OpenAI
            client=OpenAI(api_key=OPENAI_API_KEY)
            compact='\n'.join(f"{b['title']} | {b['authors']} | {b['publisher']} | {b['published']} | {b['description'][:500]}" for b in books[:12])
            prompt=f"Sei BookHunter, libraio personale in italiano. Non inventare informazioni. Profilo: {get_profile()}\nEsclusioni obbligatorie: {', '.join(exclusions())}.\nLibri da non riproporre: {saved_titles()}\nRichiesta: {topic}\nCatalogo verificato:\n{compact}\nSeleziona massimo 5 libri, motiva in base ai gusti, segnala quando i dati non bastano e non proporre saghe se non è chiaro che siano autoconclusive."
            answer=client.responses.create(model=OPENAI_MODEL,input=prompt).output_text
            await update.message.reply_text(answer[:3900])
        else:
            await update.message.reply_text('Ecco una prima selezione catalografica. Per le motivazioni AI va aggiunta la chiave OpenAI nelle variabili private del server.')
        for i,b in enumerate(books[:5],1):
            await update.message.reply_text(fmt(b,i),parse_mode='Markdown',reply_markup=book_keyboard(b),disable_web_page_preview=True)
    except Exception as e:
        log.exception('recommendation failed'); await update.message.reply_text(f'Consigli non disponibili ({type(e).__name__}).')

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
    elif data=='menu:recommend': await q.message.reply_text('Scrivi /consiglio oppure /consiglio fantascienza autoconclusiva')
    elif data=='menu:search': await q.message.reply_text('Scrivi /cerca seguito da autore, titolo o argomento.')
    elif data=='menu:library': await show_library(q.message)
    elif data=='menu:genres': await q.message.reply_text('Generi attualmente esclusi: '+(', '.join(exclusions()) or 'nessuno')+'\nPer aggiungere: /escludigenere genere\nPer rimuovere: /includigenere genere')
    elif data=='menu:releases': await releases_cmd_from_message(q.message)
    elif data=='menu:profile': await q.message.reply_text(get_profile()[:3900])

async def show_library(message, status=None):
    with db() as con:
        if status=='esclusi': rows=con.execute("SELECT title,authors,status,rating FROM books WHERE status='rejected' ORDER BY id DESC LIMIT 30").fetchall()
        elif status: rows=con.execute('SELECT title,authors,status,rating FROM books WHERE status=? ORDER BY id DESC LIMIT 30',(status,)).fetchall()
        else: rows=con.execute('SELECT title,authors,status,rating FROM books ORDER BY id DESC LIMIT 30').fetchall()
    if not rows: return await message.reply_text('Questo scaffale è vuoto.')
    labels={'wishlist':'❤️ Desidero leggere','reading':'📖 In lettura','read':'✅ Letti','suspended':'⏸ Sospesi','rejected':'🚫 Esclusi','discovered':'Scoperti'}
    lines=['📚 *La tua libreria*']
    for r in rows: lines.append(f"• {r['title']} — {r['authors']} | {labels.get(r['status'],r['status'])}"+(f" | {r['rating']}/5" if r['rating'] else ''))
    await message.reply_text('\n'.join(lines),parse_mode='Markdown')
async def library_cmd(update,context):
    if not await guard(update): return
    key=' '.join(context.args).strip().casefold(); mapping={'desidero':'wishlist','lettura':'reading','letti':'read','sospesi':'suspended','esclusi':'esclusi'}
    await show_library(update.message,mapping.get(key))

async def exclusion_cmd(update,context,kind,action):
    if not await guard(update): return
    value=' '.join(context.args).strip()
    if not value: return await update.message.reply_text('Indica il genere o autore. Esempio: /escludigenere fantasy')
    with db() as con:
        if action=='add': con.execute('INSERT OR IGNORE INTO exclusions(kind,value) VALUES(?,?)',(kind,value))
        else: con.execute('DELETE FROM exclusions WHERE kind=? AND lower(value)=lower(?)',(kind,value))
    await update.message.reply_text(('Esclusione aggiunta: ' if action=='add' else 'Esclusione rimossa: ')+value)
async def exclude_genre(u,c): await exclusion_cmd(u,c,'genre','add')
async def include_genre(u,c): await exclusion_cmd(u,c,'genre','remove')
async def exclude_author(u,c): await exclusion_cmd(u,c,'author','add')

async def profile_cmd(update,context):
    if not await guard(update): return
    await update.message.reply_text(get_profile()+'\nGeneri esclusi: '+(', '.join(exclusions()) or 'nessuno'))

async def releases_cmd_from_message(message):
    if not RSS_FEEDS: return await message.reply_text('Le fonti RSS editoriali non sono ancora configurate. Non mostro uscite non verificate.')
    found=0
    for url in RSS_FEEDS:
        feed=feedparser.parse(url)
        for entry in feed.entries[:10]:
            title=entry.get('title','Senza titolo'); link=entry.get('link',''); published=entry.get('published',entry.get('updated','Data non indicata')); key=entry.get('id',link or title)
            with db() as con:
                cur=con.execute('INSERT OR IGNORE INTO seen_releases(id,title,link,published,source,first_seen) VALUES(?,?,?,?,?,?)',(key,title,link,published,feed.feed.get('title',url),datetime.now(timezone.utc).isoformat()))
            if cur.rowcount:
                found+=1; await message.reply_text(f'🆕 {title}\n{published}\n{link}',disable_web_page_preview=True)
    if not found: await message.reply_text('Nessuna nuova voce nei feed configurati dall’ultimo controllo.')
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
    handlers=[CommandHandler('start',start),CommandHandler(['aiutami','help'],help_cmd),CommandHandler('cerca',search_cmd),CommandHandler('consiglio',recommendation_cmd),CommandHandler('wishlist',wishlist_cmd),CommandHandler('lettura',reading_cmd),CommandHandler('letto',read_cmd),CommandHandler('sospeso',suspended_cmd),CommandHandler('nonleggere',rejected_cmd),CommandHandler('libreria',library_cmd),CommandHandler('profilo',profile_cmd),CommandHandler('escludigenere',exclude_genre),CommandHandler('includigenere',include_genre),CommandHandler('escludiautore',exclude_author),CommandHandler('nuoveuscite',releases_cmd),CallbackQueryHandler(callback)]
    for h in handlers: app.add_handler(h)
    log.info('BookHunter avviato'); app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__=='__main__': main()

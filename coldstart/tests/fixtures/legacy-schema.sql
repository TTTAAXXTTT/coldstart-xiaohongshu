
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, platform TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, excerpt TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '', captured_at TEXT NOT NULL, score INTEGER NOT NULL, intent TEXT NOT NULL, evidence TEXT NOT NULL, reason TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'candidate', note TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY, platform TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, tags TEXT NOT NULL, cards TEXT NOT NULL, assets TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL DEFAULT 'draft', generator TEXT NOT NULL, source_ids TEXT NOT NULL DEFAULT '[]', scheduled_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '', public_url TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, message TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL, platform TEXT NOT NULL, started_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'started');
            
ALTER TABLE leads ADD COLUMN analyzed_at TEXT NOT NULL DEFAULT '';

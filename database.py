import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("eclipse_rp.sqlite3")

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            tickets_category_id INTEGER,
            logs_channel_id INTEGER,
            applications_channel_id INTEGER,
            verified_role_id INTEGER,
            unverified_role_id INTEGER,
            staff_role_id INTEGER,
            welcome_channel_id INTEGER,
            announcements_channel_id INTEGER,
            verify_channel_id INTEGER,
            ticket_panel_channel_id INTEGER,
            application_panel_channel_id INTEGER,
            verify_panel_channel_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS tickets (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            opener_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            claimed_by INTEGER,
            opened_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS invites (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            inviter_id INTEGER,
            invite_code TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS member_stats (
            guild_id INTEGER PRIMARY KEY,
            joins INTEGER DEFAULT 0,
            leaves INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS custom_commands (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            response TEXT NOT NULL,
            PRIMARY KEY (guild_id, name)
        );
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            answers TEXT NOT NULL,
            status TEXT DEFAULT 'En attente',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # Migration douce pour les bases créées avec une version précédente.
        cols = {row["name"] for row in con.execute("PRAGMA table_info(guild_config)")}
        defaults = {
            "tickets_category_id": "INTEGER", "logs_channel_id": "INTEGER",
            "applications_channel_id": "INTEGER", "verified_role_id": "INTEGER",
            "unverified_role_id": "INTEGER", "staff_role_id": "INTEGER",
            "welcome_channel_id": "INTEGER", "announcements_channel_id": "INTEGER",
            "verify_channel_id": "INTEGER", "ticket_panel_channel_id": "INTEGER",
            "application_panel_channel_id": "INTEGER", "verify_panel_channel_id": "INTEGER"
        }
        for name, typ in defaults.items():
            if name not in cols:
                con.execute(f"ALTER TABLE guild_config ADD COLUMN {name} {typ}")

def get_config(guild_id):
    with connect() as con:
        row = con.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else {"guild_id": guild_id}

def set_config(guild_id, field, value):
    allowed = {
        "tickets_category_id", "logs_channel_id", "applications_channel_id",
        "verified_role_id", "unverified_role_id", "staff_role_id",
        "welcome_channel_id", "announcements_channel_id", "verify_channel_id",
        "ticket_panel_channel_id", "application_panel_channel_id", "verify_panel_channel_id"
    }
    if field not in allowed:
        raise ValueError("Configuration inconnue")
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        con.execute(f"UPDATE guild_config SET {field}=? WHERE guild_id=?", (value, guild_id))

def add_ticket(channel_id, guild_id, opener_id, category):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO tickets(channel_id,guild_id,opener_id,category) VALUES(?,?,?,?)",
            (channel_id, guild_id, opener_id, category)
        )

def get_ticket(channel_id):
    with connect() as con:
        row = con.execute("SELECT * FROM tickets WHERE channel_id=?", (channel_id,)).fetchone()
        return dict(row) if row else None

def claim_ticket(channel_id, user_id):
    with connect() as con:
        row = con.execute("SELECT claimed_by FROM tickets WHERE channel_id=?", (channel_id,)).fetchone()
        if not row:
            return False, "Ce ticket n'est pas enregistré."
        if row["claimed_by"] is not None:
            return False, "Ce ticket est déjà pris en charge."
        con.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=?", (user_id, channel_id))
        return True, "Ticket pris en charge."

def close_ticket(channel_id):
    with connect() as con:
        con.execute("UPDATE tickets SET closed_at=CURRENT_TIMESTAMP WHERE channel_id=?", (channel_id,))

def record_join(guild_id):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO member_stats(guild_id) VALUES(?)", (guild_id,))
        con.execute("UPDATE member_stats SET joins=joins+1 WHERE guild_id=?", (guild_id,))

def record_leave(guild_id):
    with connect() as con:
        con.execute("INSERT OR IGNORE INTO member_stats(guild_id) VALUES(?)", (guild_id,))
        con.execute("UPDATE member_stats SET leaves=leaves+1 WHERE guild_id=?", (guild_id,))

def get_stats(guild_id):
    with connect() as con:
        row = con.execute("SELECT joins, leaves FROM member_stats WHERE guild_id=?", (guild_id,)).fetchone()
        tickets = con.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE guild_id=? AND closed_at IS NULL", (guild_id,)
        ).fetchone()["n"]
        applications = con.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE guild_id=? AND status='En attente'", (guild_id,)
        ).fetchone()["n"]
        if not row:
            return {"joins": 0, "leaves": 0, "tickets": tickets, "applications": applications}
        return {"joins": row["joins"], "leaves": row["leaves"], "tickets": tickets, "applications": applications}

def save_invite(guild_id, user_id, inviter_id, code):
    with connect() as con:
        con.execute(
            "INSERT INTO invites(guild_id,user_id,inviter_id,invite_code) VALUES(?,?,?,?)",
            (guild_id, user_id, inviter_id, code)
        )

def invite_top(guild_id, limit=10):
    with connect() as con:
        return con.execute("""
            SELECT inviter_id, COUNT(*) AS total FROM invites
            WHERE guild_id=? AND inviter_id IS NOT NULL
            GROUP BY inviter_id ORDER BY total DESC LIMIT ?
        """, (guild_id, limit)).fetchall()

def add_custom_command(guild_id, name, response):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO custom_commands(guild_id,name,response) VALUES(?,?,?)",
            (guild_id, name.lower(), response)
        )

def get_custom_command(guild_id, name):
    with connect() as con:
        row = con.execute(
            "SELECT response FROM custom_commands WHERE guild_id=? AND name=?",
            (guild_id, name.lower())
        ).fetchone()
        return row["response"] if row else None

def create_application(guild_id, user_id, answers):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO applications(guild_id,user_id,answers) VALUES(?,?,?)",
            (guild_id, user_id, answers)
        )
        return cur.lastrowid

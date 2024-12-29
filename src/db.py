import sqlite3

DB_FILE = "discord_bot.db"


def initialize_database():
    """Initialize the SQLite database and create tables if they do not exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Create a table to store server IDs
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS servers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT NOT NULL UNIQUE
        )
        """
    )

    # Create a table to store channel links
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            text_channel_id TEXT NOT NULL,
            voice_channel_id TEXT NOT NULL UNIQUE,
            role_id TEXT,
            FOREIGN KEY (guild_id) REFERENCES servers(server_id)
        )
        """
    )

    conn.commit()
    conn.close()


def add_server(server_id):
    """Add a server ID to the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        cursor.execute("INSERT INTO servers (server_id) VALUES (?)", (server_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        print(f"Server ID {server_id} already exists.")
    finally:
        conn.close()


def get_servers():
    """Fetch all server IDs from the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM servers")
    servers = cursor.fetchall()
    conn.close()

    return servers


def add_channel_link(guild_id, text_channel_id, voice_channel_id, role_id=None):
    """Add a channel link to the database."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO channel_links (guild_id, text_channel_id, voice_channel_id, role_id)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, text_channel_id, voice_channel_id, role_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError("The specified voice channel is already linked.")
    finally:
        conn.close()


def get_channel_link(voice_channel_id):
    """Retrieve a channel link by voice channel ID."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT guild_id, text_channel_id, role_id
        FROM channel_links
        WHERE voice_channel_id = ?
        """,
        (voice_channel_id,),
    )

    result = cursor.fetchone()
    conn.close()

    return result


def remove_channel_link(link_id):
    """Remove a channel link by its ID."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM channel_links
        WHERE id = ?
        """,
        (link_id,),
    )

    conn.commit()
    conn.close()


def get_channel_links_by_guild(guild_id):
    """Retrieve all channel links for a specific guild."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, text_channel_id, voice_channel_id, role_id
        FROM channel_links
        WHERE guild_id = ?
        """,
        (guild_id,),
    )

    links = cursor.fetchall()
    conn.close()

    return links

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

import sqlite3
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(script_dir, "discord_bot.db")


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

    # Create a table to store custom messages
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            type TEXT NOT NULL,
            message TEXT NOT NULL,
            FOREIGN KEY (guild_id) REFERENCES servers(server_id),
            UNIQUE(guild_id, type)
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


def update_channel_link_text(voice_channel_id, new_text_channel_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE channel_links
        SET text_channel_id = ?
        WHERE voice_channel_id = ?
        """,
        (new_text_channel_id, voice_channel_id),
    )

    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()

    return updated


def update_channel_link_role(voice_channel_id, new_role_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE channel_links
        SET role_id = ?
        WHERE voice_channel_id = ?
        """,
        (new_role_id, voice_channel_id),
    )

    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()

    return updated


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


def set_custom_message(guild_id, msg_type, message):
    """Set a custom message for a specific guild and type (join/leave/move).
    If message is None, it will remove the custom message and revert to default."""
    if msg_type not in ["join", "leave", "move"]:
        raise ValueError("Message type must be 'join', 'leave', or 'move'")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    if message is None:
        # Remove the custom message to revert to default
        cursor.execute(
            """
            DELETE FROM custom_messages
            WHERE guild_id = ? AND type = ?
            """,
            (guild_id, msg_type),
        )
    else:
        cursor.execute(
            """
            INSERT INTO custom_messages (guild_id, type, message)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, type)
            DO UPDATE SET message = excluded.message
            """,
            (guild_id, msg_type, message),
        )

    conn.commit()
    conn.close()


def get_custom_message(guild_id, msg_type):
    """Get a custom message for a specific guild and type."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT message
        FROM custom_messages
        WHERE guild_id = ? AND type = ?
        """,
        (guild_id, msg_type),
    )

    result = cursor.fetchone()
    conn.close()

    return result[0] if result else None

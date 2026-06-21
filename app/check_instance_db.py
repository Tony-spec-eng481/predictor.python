import sqlite3
import os

db_path = 'instance/database.db'
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        print(f"Tables: {tables}")
        if ('road_worx_round',) in tables:
            cursor.execute("SELECT * FROM road_worx_round ORDER BY timestamp DESC LIMIT 10;")
            rows = cursor.fetchall()
            print(f"Found {len(rows)} rounds.")
            for row in rows:
                print(row)
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
else:
    print("Database not found.")

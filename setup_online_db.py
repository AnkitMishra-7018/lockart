"""
Creates the 13 tables in the database that the environment variables point to
(use it for the online Aiven database). It reads schema.sql, skips the
CREATE DATABASE / USE lines (the online database already exists) and runs the rest.

Safe to run twice: tables that already exist are skipped.
"""
import mysql.connector

from db import get_connection


def read_statements(path="schema.sql"):
    lines = []
    for line in open(path, encoding="utf-8"):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if stripped.upper().startswith(("CREATE DATABASE", "USE ")):
            continue
        lines.append(line)
    return [part.strip() for part in "".join(lines).split(";") if part.strip()]


conn = get_connection()
cursor = conn.cursor()
created = skipped = 0
for statement in read_statements():
    try:
        cursor.execute(statement)
        created += 1
    except mysql.connector.Error as error:
        if error.errno == 1050:                      # table already exists
            skipped += 1
        else:
            raise
conn.commit()
cursor.close()
conn.close()
print("Tables created:", created, "| already existed:", skipped)
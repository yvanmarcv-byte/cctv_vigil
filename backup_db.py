import os
import sys

try:
    import psycopg2
except ImportError:
    print("Installing psycopg2...")
    os.system(f"{sys.executable} -m pip install psycopg2-binary")
    import psycopg2

url = os.environ.get("DATABASE_URL", "")
if not url:
    print("Set DATABASE_URL first")
    sys.exit(1)

# Remove ?sslmode=require if present for psycopg2
if "?" in url:
    base, params = url.split("?", 1)
    url = base

conn = psycopg2.connect(url)
cur = conn.cursor()

# Get all tables
cur.execute("""
    SELECT table_name FROM information_schema.tables 
    WHERE table_schema = 'public'
""")
tables = [row[0] for row in cur.fetchall()]

print(f"Found {len(tables)} tables: {tables}")

with open("backup.sql", "w") as f:
    f.write("-- CCTV Vigil Database Backup\n")
    f.write(f"-- Tables: {', '.join(tables)}\n\n")
    
    for table in tables:
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
        
        f.write(f"-- Table: {table} ({len(rows)} rows)\n")
        f.write(f"DELETE FROM {table};\n")
        
        for row in rows:
            values = []
            for val in row:
                if val is None:
                    values.append("NULL")
                elif isinstance(val, str):
                    values.append(f"'{val.replace(chr(39), chr(39)+chr(39))}'")
                elif isinstance(val, bool):
                    values.append("TRUE" if val else "FALSE")
                else:
                    values.append(str(val))
            f.write(f"INSERT INTO {table} ({', '.join(col_names)}) VALUES ({', '.join(values)});\n")
        f.write("\n")

cur.close()
conn.close()
print("Backup saved to backup.sql")

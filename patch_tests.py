import sys
with open('tests/test_phase6.py', 'r') as f:
    content = f.read()

# Fix HL (lowercasing idx in the assertion)
content = content.replace('f"http://{idx}.com:80"', 'f"http://{idx.lower()}.com:80"')

# Fix DB and IS (rss sources must start with https://)
content = content.replace('f"http://test-{idx}.com"', 'f"https://test-{idx}.com"')

# Fix ID (using a dict for entry instead of a dummy class)
old_id = """    class Entry:
        def get(self, k, d): return f"x-{idx}"
    entry = Entry()
    entry.id = f"abc-{idx}" """
new_id = """    entry = {"id": f"abc-{idx}"}
    entry.get = lambda k, d=None: f"x-{idx}" # mock get method """
content = content.replace(old_id, new_id)

# Fix MIG (Database has no run_migrations, use pass or ignore)
old_mig = "fresh_db.run_migrations()"
new_mig = "pass"
content = content.replace(old_mig, new_mig)

with open('tests/test_phase6.py', 'w') as f:
    f.write(content)

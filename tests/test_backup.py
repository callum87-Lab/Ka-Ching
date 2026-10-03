"""Backups and restores."""
from .conftest import add_item


def test_backup_and_restore_round_trip(client, seeded):
    backup = client.get("/settings/backup")
    assert backup.status_code == 200 and backup.content[:16] == b"SQLite format 3\x00"
    add_item(seeded, "Added After Backup #1", 1.00, 5)
    assert "Added After Backup #1" in client.get("/search?q=Added").text
    r = client.post("/settings/restore", files={"backup_file": ("b.db", backup.content)}, data={"next": "/settings"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Added After Backup #1" not in client.get("/search?q=Added").text
    assert "Test Series #1" in client.get("/search?q=Test").text


def test_restore_rejects_a_non_backup(client, seeded):
    client.post("/settings/restore", files={"backup_file": ("x.db", b"not a database")}, data={"next": "/settings"})
    assert "Test Series #1" in client.get("/search?q=Test").text

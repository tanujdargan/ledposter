import yaml


def test_hand_edit_survives_a_save_from_the_app(authed, data):
    """Editing config.yaml while the app runs, then saving anything from the app,
    keeps the hand edit (the app reloads instead of writing its stale copy back)."""
    client, path = authed, data / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["matrix"]["panels"] = [{"col": 0, "row": 0, "rotate": 180}, {"col": 1, "row": 0, "rotate": 90}]
    path.write_text(yaml.safe_dump(cfg))
    s = client.get("/api/settings").json()
    assert s["matrix"]["panels"][1]["rotate"] == 90
    assert client.put("/api/settings", json=s).status_code == 200
    assert yaml.safe_load(path.read_text())["matrix"]["panels"][1]["rotate"] == 90

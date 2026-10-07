from fastapi.testclient import TestClient

from conftest import PASSWORD
from ledposter import auth
from ledposter.app import create_app


def test_unauthenticated_is_401(authed):
    anon = TestClient(authed.app)  # no cookie
    for path in ("/api/status", "/api/media", "/api/playlists", "/api/schedule", "/api/settings", "/api/tokens"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/api/brightness", json={"value": 10}).status_code == 401
    assert anon.get("/api/auth/state").json() == {"first_run": False, "authenticated": False}
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200
    assert anon.get("/static/app.css").status_code == 200  # assets the login page needs stay public


def test_first_run_flow(client):
    assert client.get("/api/auth/state").json() == {"first_run": True, "authenticated": False}
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 409
    assert client.post("/api/auth/setup", json={"password": "short"}).status_code == 422
    r = client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200 and "session" in r.cookies
    assert client.get("/api/auth/state").json() == {"first_run": False, "authenticated": True}
    assert client.get("/api/status").status_code == 200
    # once set, the first-run endpoint is closed, even for the admin
    assert client.post("/api/auth/setup", json={"password": "another password"}).status_code == 409
    client.post("/api/auth/logout")
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/auth/login", json={"password": "wrong password"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/status").status_code == 200


def test_password_is_hashed_and_sessions_survive_restart(authed, data):
    text = (data / "config.yaml").read_text()
    assert PASSWORD not in text and "scrypt$" in text
    cookie = authed.cookies["session"]
    with TestClient(create_app(str(data))) as again:  # same secret file: the cookie still works
        again.cookies.set("session", cookie)
        assert again.get("/api/status").status_code == 200


def test_token_auth_and_revoke(authed):
    r = authed.post("/api/tokens", json={"name": "home-assistant"})
    assert r.status_code == 201
    tok = r.json()
    assert tok["token"].startswith("lp_") and tok["name"] == "home-assistant"
    assert "token" not in authed.get("/api/tokens").json()[0]  # never shown again
    cookie = authed.cookies["session"]
    authed.cookies.clear()  # same client (same event loop as the app), token only
    bearer = {"Authorization": f"Bearer {tok['token']}"}
    assert authed.get("/api/status").status_code == 401
    assert authed.get("/api/status", headers=bearer).status_code == 200
    assert authed.post("/api/brightness", json={"value": 42}, headers=bearer).status_code == 200
    assert authed.get("/api/status", headers={"Authorization": "Bearer lp_nope"}).status_code == 401
    authed.cookies.set("session", cookie)
    assert authed.delete(f"/api/tokens/{tok['id']}").status_code == 200
    authed.cookies.clear()
    assert authed.get("/api/status", headers=bearer).status_code == 401  # revoked
    authed.cookies.set("session", cookie)
    assert authed.get("/api/tokens").json() == []
    assert authed.delete(f"/api/tokens/{tok['id']}").status_code == 404


def test_login_rate_limit(authed):
    authed.post("/api/auth/logout")
    for _ in range(5):
        assert authed.post("/api/auth/login", json={"password": "wrong password"}).status_code == 401
    assert authed.post("/api/auth/login", json={"password": "wrong password"}).status_code == 429
    assert authed.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429  # even the right one


def test_password_change_signs_out_other_sessions(authed):
    other = TestClient(authed.app)
    other.post("/api/auth/login", json={"password": PASSWORD})
    assert other.get("/api/schedule").status_code == 200
    assert authed.post("/api/auth/password", json={"current": "nope nope nope", "new": "new password 1"}).status_code == 403
    assert authed.post("/api/auth/password", json={"current": PASSWORD, "new": "new password 1"}).status_code == 200
    assert authed.get("/api/status").status_code == 200  # the changing session got a fresh cookie
    assert other.get("/api/schedule").status_code == 401
    assert other.post("/api/auth/login", json={"password": "new password 1"}).status_code == 200


def test_primitives():
    h = auth.hash_password("pw")
    assert h.startswith("scrypt$16384$8$1$") and auth.verify_password("pw", h) and not auth.verify_password("pW", h)
    assert h != auth.hash_password("pw")  # random salt
    assert not auth.verify_password("pw", "garbage")
    s = auth.Sessions(b"k" * 32, ttl=100)
    c = s.issue(h)
    assert s.verify(c, h) and not s.verify(c + "x", h) and not s.verify(c, auth.hash_password("pw"))
    assert not auth.Sessions(b"j" * 32).verify(c, h)
    assert not auth.Sessions(b"k" * 32, ttl=-1).verify(auth.Sessions(b"k" * 32, ttl=-1).issue(h), h)
    rl = auth.RateLimiter(limit=2, window=10)
    rl.fail("a", now=0), rl.fail("a", now=5)
    assert rl.blocked("a", now=6) and not rl.blocked("b", now=6) and not rl.blocked("a", now=11)

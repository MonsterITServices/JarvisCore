import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import app
from integrations import auth


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "JARVIS_AUTH_DB": str(Path(self.directory.name) / "auth.sqlite3"),
            "JARVIS_PUBLIC_ORIGIN": "https://jarvis.example.com",
        })
        self.environment.start()
        auth.add_user("alice", "correct horse battery")
        self.client = app.test_client()
        self.headers = {"Origin": "http://localhost"}

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def sign_in(self):
        return self.client.post("/api/login", headers=self.headers, json={"username": "alice", "password": "correct horse battery"})

    def test_authentication_and_user_revocation(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/login", headers=self.headers, json={"username": "alice", "password": "wrong"}).status_code, 401)
        self.assertIn("HttpOnly", self.sign_in().headers["Set-Cookie"])
        self.assertEqual(self.client.post("/api/settings/users", headers={"Origin": "https://evil.example"}, json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/settings/users", headers=self.headers, json={"username": "bob", "password": "another strong password"}).status_code, 201)
        self.assertEqual(self.client.delete("/api/settings/users/alice", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/settings/users").status_code, 401)
        with self.assertRaises(auth.AuthError):
            auth.remove_user("bob")

    def test_tunnel_cookie_and_transport(self):
        payload = {"username": "alice", "password": "correct horse battery"}
        response = self.client.post("/api/login", base_url="http://jarvis.example.com", headers={"Origin": "https://jarvis.example.com"}, json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertEqual(self.client.post("/api/login", base_url="http://192.168.1.50:8080", headers={"Origin": "http://192.168.1.50:8080"}, json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/login", base_url="http://other.example", headers={"Origin": "https://jarvis.example.com"}, json=payload).status_code, 403)

    def test_logout_and_last_user(self):
        self.sign_in()
        self.assertEqual(self.client.delete("/api/settings/users/alice", headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/api/logout", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/settings/users").status_code, 401)


if __name__ == "__main__":
    unittest.main()